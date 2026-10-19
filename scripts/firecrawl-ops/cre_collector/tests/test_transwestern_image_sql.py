"""SQL assembly only; actual transaction semantics use the owned PG contract."""

import copy

import pytest

import cre_ingest as ingest
from cre_transwestern_image_sql import build_parts, image_state_query


def row():
    url = "https://transwestern.com/property/example"
    return ingest.to_row({
        "sourceKey": "transwestern", "id": "example", "url": url,
        "canonicalUrl": url, "name": "Example", "transactionType": "Sale",
        "photos": ["https://images.transwestern.com/property/photo.jpg"],
        "rawTranswesternFeed": {"PropertyImage": "https://images.transwestern.com/property/photo.jpg"},
        "transwesternPhotoRoleVersion": "v1", "detailObservedAt": "2026-10-19T20:00:00Z",
        "freshnessProvenance": {"method": "transwestern_detail_page", "cacheDisposition": "live"},
        "transwesternPrimaryPhotoEvidence": {"url": "https://images.transwestern.com/property/photo.jpg", "basis": "provider_feed"},
    }, {}, "2026-10-19T20:00:00Z")


def binding():
    return {
        "artifact_sha256": "a" * 64, "artifact_bytes": 100,
        "artifact_run_key": "ingest:v1:test", "generation_id": "fixture",
        "generation_started_at": "2026-10-19T20:00:00Z",
        "database_target_sha256": "b" * 64, "attempt_id": "c" * 32,
        "collector_git_sha": "d" * 40,
    }


def test_parts_have_explicit_transaction_boundaries_and_do_not_mutate_input():
    rows, claim = [row()], binding()
    original = copy.deepcopy((rows, claim))
    parts = build_parts(rows, claim)
    assert set(parts) == {"before", "project", "verify", "emit"}
    assert (rows, claim) == original
    assert "ON COMMIT PRESERVE ROWS" in parts["before"]
    assert "_child_counts_by_listing_before" in parts["before"]
    assert "UPDATE _src" in parts["project"]
    assert "UPDATE credeals.cre_listing_images" not in parts["project"]
    assert "EXCEPT ALL" in parts["verify"]
    assert "INTERSECT ALL" in parts["verify"]
    assert "_retained_child_scope_after" in parts["verify"]
    assert "INSERT INTO _tw_image_outcome" in parts["verify"]
    assert "CRE_TRANSWESTERN_IMAGE_TRANSITION " in parts["emit"]
    assert "CRE_TRANSWESTERN_IMAGE_TRANSITION " not in "".join(parts[key] for key in ("before", "project", "verify"))
    assert all("COMMIT;" not in value for value in parts.values())


@pytest.mark.parametrize("mutate", [
    lambda rows, claim: rows.clear(),
    lambda rows, claim: rows[0].update(slug="savills"),
    lambda rows, claim: rows.append(copy.deepcopy(rows[0])),
    lambda rows, claim: rows[0]["raw_data"].pop("transwesternPhotoRoleVersion"),
    lambda rows, claim: claim.pop("collector_git_sha"),
    lambda rows, claim: claim.update(collector_git_sha="not-a-sha"),
    lambda rows, claim: claim.update(artifact_bytes=True),
    lambda rows, claim: claim.update(extra="unknown"),
])
def test_unbound_mixed_duplicate_or_malformed_inputs_refuse(mutate):
    rows, claim = [row()], binding()
    mutate(rows, claim)
    with pytest.raises(ValueError):
        build_parts(rows, claim)


def test_none_is_not_authority_inside_the_sql_module():
    with pytest.raises(ValueError):
        build_parts([row()], None)


def test_fingerprint_uses_physical_rows_and_builtin_sha256_without_ddl():
    query = image_state_query()
    assert "image_count" in query and "image_state_sha256" in query
    assert "sha256" in query and "convert_to" in query
    assert "is_primary" in query and "display_order" in query and "alt_text" in query
    assert "jsonb_build_array" in query and "ORDER BY" in query
    assert "DISTINCT" not in query
    assert "CREATE" not in query and "pgcrypto" not in query
    assert "b.slug='transwestern'" in query and "l.deleted_at IS NULL" in query


def test_old_removal_policy_is_exact_and_current_retained_urls_win():
    before = build_parts([row()], binding())["before"]
    assert "https://transwestern.com/assets/images/transwestern-mapmarker.png" in before
    assert "mapsresources-pa" in before and "v1/tiles" in before
    assert "current.url IS NULL" in before and "jsonb_array_elements(input.images)" in before
    assert "jsonb_array_elements(input.excluded)" in before


def test_role_and_alt_projection_are_explicit_without_reordering_source_images():
    parts = build_parts([row()], binding())
    assert "WITH ORDINALITY" in parts["before"]
    assert "current.display_order" in parts["project"]
    assert "ORDER BY projection.display_order" in parts["project"]
    assert "historical_primary" in parts["project"]
    assert "current_source_primary" in parts["project"]
    assert "unassigned_primary" in parts["project"]
    assert "ambiguous" in parts["project"]
    assert "altText" in parts["project"]
    assert "alt_text" in parts["verify"]


def test_projection_groups_history_and_stage_once_instead_of_per_image_scans():
    parts = build_parts([row()], binding())
    assert parts["before"].count("jsonb_array_elements(input.images)") == 1
    assert "GROUP BY external_id,url" in parts["before"]
    assert "GROUP BY p.external_id,prior.url" in parts["project"]
    assert "LEFT JOIN _tw_image_history history USING(external_id,url)" in parts["project"]
    assert "current.display_order=urls.first_order" in parts["project"]
    assert "GROUP BY external_id" in parts["project"]
    assert "(SELECT min(" not in parts["project"]
    assert "projection.external_id=s.external_id" not in parts["project"]
