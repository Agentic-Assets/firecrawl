"""Canonical writer hook placement; physical guards live in the PG contract."""

import hashlib
import inspect
import sys
from types import SimpleNamespace

import cre_ingest as ingest


def test_bound_tw_parts_surround_actual_canonical_child_writer(monkeypatch):
    row = ingest.to_row({
        "sourceKey": "transwestern", "id": "fixture", "name": "Fixture property",
        "url": "https://transwestern.com/property/fixture",
        "canonicalUrl": "https://transwestern.com/property/fixture",
        "photos": ["https://images.transwestern.com/property/fixture.jpg"],
    }, {}, "2026-10-19T20:00:00Z")
    assert row is not None
    markers = {name: f"-- TW_CONTRACT_{name.upper()}" for name in ("before", "project", "verify", "emit")}
    calls = []

    def parts(rows, binding):
        calls.append((rows, binding))
        return markers

    monkeypatch.setitem(sys.modules, "cre_transwestern_image_sql", SimpleNamespace(build_parts=parts))
    run_key, _ = ingest.artifact_run_identity_from_digests([hashlib.sha256(b"fixture").hexdigest()])
    binding = {
        "artifact_sha256": hashlib.sha256(b"fixture").hexdigest(), "artifact_bytes": 7,
        "artifact_run_key": run_key, "generation_id": "fixture-4576",
        "generation_started_at": "2026-10-19T20:00:00Z", "database_target_sha256": "b" * 64,
        "attempt_id": "c" * 32, "collector_git_sha": "d" * 40,
    }
    options = {"artifact_run_key": run_key, "skip_post_commit_summary": True}
    # On the unchanged writer, execute its ordinary SQL generator: RED must be
    # a missing integration assertion, not an unsupported-keyword TypeError.
    if "transwestern_image_transition_binding" in inspect.signature(ingest.build_sql).parameters:
        options["transwestern_image_transition_binding"] = binding
    sql = ingest.build_sql([row], [], "2026-10-19T20:00:00Z", set(), **options)
    assert all(marker in sql for marker in markers.values()), "canonical writer omitted TW guard/projection/outcome hooks"
    assert len(calls) == 1 and calls[0][1] == binding
    assert sql.index(markers["before"]) < sql.index(markers["project"])
    assert sql.index(markers["project"]) < sql.index("DELETE FROM credeals.cre_listing_images")
    assert sql.index("INSERT INTO credeals.cre_listing_images") < sql.index(markers["verify"])
    assert sql.index(markers["verify"]) < sql.rindex("COMMIT;") < sql.index(markers["emit"])
