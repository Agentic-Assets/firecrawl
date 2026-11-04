"""Synthetic validation tests for the finite protected lifecycle context."""

import hashlib
import json
import os
from dataclasses import FrozenInstanceError

import cre_lifecycle_protected_context as protected
import pytest

BROKERAGE = "20000000-0000-4000-8000-000000000001"
LISTING_IDS = tuple(
    f"10000000-0000-4000-8000-{number:012d}" for number in range(1, 5)
)
INDEX_IDS = tuple(
    f"30000000-0000-4000-8000-{number:012d}" for number in range(1, 5)
)
URLS = (
    "https://invest.jll.com/us/en/listings/synthetic-a",
    "https://invest.jll.com/us/en/listings/synthetic-b",
)
EXTERNAL_IDS = ("investor:AA1", "investor:AA2", "investor:BB1", "investor:BB2")
OBSERVED_AT = "2026-10-19T18:00:00Z"
EVIDENCE_SHA = "a" * 64
TARGET_SHA = "b" * 64


def _catalog_row(table, columns):
    return {
        "schema": "credeals",
        "table": table,
        "relkind": "r",
        "row_security": False,
        "force_row_security": False,
        "columns": [
            {
                "position": position,
                "name": name,
                "type": "text",
                "not_null": False,
                "identity": "",
                "generated": "",
                "default": None,
            }
            for position, name in enumerate(columns, start=1)
        ],
        "constraints": [],
        "indexes": [],
        "triggers": [],
        "policies": [],
    }


def _snapshot():
    columns = {table: ["id"] for table in protected.TABLES}
    columns.update({
        "cre_listings": [
            "id", "external_id", "brokerage_id", "status", "deleted_at",
            "transaction_type", "source_url", "canonical_url", "raw_data",
        ],
        "cre_brokerages": ["id", "slug"],
        "cre_source_index": [
            "id", "external_id", "brokerage_id", "source_key", "url",
            "soft_deleted", "observation_present", "presence_generation",
        ],
        "cre_source_baseline": ["source_key", "brokerage_slug"],
    })
    objects = {table: [] for table in protected.TABLES}
    for number, (listing_id, external_id) in enumerate(zip(LISTING_IDS, EXTERNAL_IDS)):
        url = URLS[number // 2]
        objects["cre_listings"].append({
            "id": listing_id,
            "external_id": external_id,
            "brokerage_id": BROKERAGE,
            "status": "active",
            "deleted_at": None,
            "transaction_type": "sale",
            "source_url": url,
            "canonical_url": url,
            "raw_data": {"opaque": "guard's \\\\ marker", "number": "__RAW_NUMBER__"},
        })
        objects["cre_source_index"].append({
            "id": INDEX_IDS[number],
            "external_id": external_id,
            "brokerage_id": BROKERAGE,
            "source_key": "jll-investor",
            "url": url,
            "soft_deleted": False,
            "observation_present": True,
            "presence_generation": 6 + number,
        })
    objects["cre_brokerages"] = [{"id": BROKERAGE, "slug": "jll"}]
    objects["cre_source_baseline"] = [
        {"source_key": source_key, "brokerage_slug": "jll"}
        for source_key in ("jll", "jll-investor")
    ]
    return {
        "schema": protected.SNAPSHOT_SCHEMA,
        "payload": {
            "objects": objects,
            "catalog": [
                _catalog_row(table, columns[table]) for table in protected.TABLES
            ],
        },
        "isolation": "repeatable read",
        "read_only": "on",
        "observed_at": OBSERVED_AT,
        "matched_rows": 4,
        "expected_rows": 4,
        "source_acceptance": False,
        "server_version_num": "160005",
        "evidence_time_semantics": "retained_database_evidence_observed_at",
        "complete_exact_identity_scope": True,
    }


def _actions():
    actions = []
    for number in (0, 2):
        generation = 6 + number
        before = {
            "listing_deleted": False,
            "listing_status": "active",
            "index_soft_deleted": False,
            "observation_present": True,
            "presence_generation": generation,
        }
        before_hash = hashlib.sha256(
            "\x1f".join(("false", "active", "false", "true", str(generation))).encode()
        ).hexdigest()
        actions.append({
            "listing_id": LISTING_IDS[number],
            "brokerage_id": BROKERAGE,
            "external_id": EXTERNAL_IDS[number],
            "source_key": "jll-investor",
            "source_url": URLS[number // 2],
            "source_evidence_path": "/synthetic/source-artifact.json",
            "source_evidence_sha256": EVIDENCE_SHA,
            "observed_at": OBSERVED_AT,
            "before": before,
            "before_hash": before_hash,
            "desired_present": False,
            "desired_status": "inactive",
            "desired_generation": generation + 1,
            "lifecycle_change": True,
            "history": None,
            "operations": ["lifecycle_sync", "lifecycle_event"],
            "event": {
                "event_type": "disappeared",
                "presence_generation": generation + 1,
                "provenance": "approved_lifecycle_state_reconciliation",
                "evidence_observed_at_semantics": (
                    "source_state_observed_at_not_transition_time"
                ),
                "require_reconciliation_provenance": False,
            },
        })
    return actions


def _write_snapshot(tmp_path, snapshot=None, *, raw_text=None):
    path = tmp_path / "synthetic-protected-context.json"
    if raw_text is None:
        raw_text = json.dumps(snapshot or _snapshot(), sort_keys=True, separators=(",", ":"))
        raw_text = raw_text.replace(
            '"__RAW_NUMBER__"', "900719925474099312345.6789",
        )
        raw_text += "\n"
    path.write_text(raw_text, encoding="utf-8")
    pointer = {
        "snapshot_path": path.name,
        "snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "database_target_sha256": TARGET_SHA,
    }
    return path, pointer


def test_load_context_accepts_synthetic_four_parent_scope_and_returns_fresh_views(tmp_path):
    path, pointer = _write_snapshot(tmp_path)

    context = protected.load_context(pointer, _actions(), evidence_base_dir=tmp_path)

    assert context.binding == {
        "schema": protected.SCHEMA,
        "snapshot_path": str(path.resolve()),
        "snapshot_sha256": pointer["snapshot_sha256"],
        "database_target_sha256": TARGET_SHA,
    }
    parents = context.non_action_parents
    assert len(parents) == 2
    assert {parent["external_id"] for parent in parents} == {
        "investor:AA2", "investor:BB2",
    }
    assert all(parent["brokerage_id"] == BROKERAGE for parent in parents)
    assert all(set(parent) == {"listing_id", "brokerage_id", "external_id", "source_url"}
               for parent in parents)
    parents[0]["external_id"] = "mutated-view"
    assert "mutated-view" not in {parent["external_id"] for parent in context.non_action_parents}
    binding = context.binding
    binding["database_target_sha256"] = "0" * 64
    assert context.binding["database_target_sha256"] == TARGET_SHA
    with pytest.raises(FrozenInstanceError):
        context._sha256 = "0" * 64

    guard = context.guard_sql()
    ordered_tables = ", ".join("credeals." + table for table in protected.TABLES)
    assert f"LOCK TABLE {ordered_tables} IN SHARE ROW EXCLUSIVE MODE;" in guard
    assert guard.index("SET LOCAL lock_timeout") < guard.index("LOCK TABLE")
    assert "900719925474099312345.6789" in guard
    assert "guard''''s" in guard
    assert context.guard_sql() == guard


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_only", True),
        ("source_acceptance", True),
        ("complete_exact_identity_scope", False),
        ("expected_rows", True),
    ],
)
def test_load_context_rejects_inexact_snapshot_metadata(tmp_path, field, value):
    snapshot = _snapshot()
    snapshot[field] = value
    _, pointer = _write_snapshot(tmp_path, snapshot)

    with pytest.raises(ValueError, match="protected context"):
        protected.load_context(pointer, _actions(), evidence_base_dir=tmp_path)


def test_load_context_rejects_duplicate_keys_and_action_drift(tmp_path):
    _, pointer = _write_snapshot(
        tmp_path, raw_text='{"schema":"wrong","schema":"wrong-again"}',
    )
    with pytest.raises(ValueError, match="protected context: duplicate JSON key"):
        protected.load_context(pointer, _actions(), evidence_base_dir=tmp_path)

    _, pointer = _write_snapshot(tmp_path)
    for field, changed in (
        ("source_evidence_path", "/synthetic/other-artifact.json"),
        ("source_evidence_sha256", "c" * 64),
        ("observed_at", "2026-10-19T18:01:00Z"),
    ):
        actions = _actions()
        actions[1][field] = changed
        with pytest.raises(ValueError, match="one exact source artifact"):
            protected.load_context(pointer, actions, evidence_base_dir=tmp_path)


def test_load_context_rejects_unknown_pointer_key_and_symlink(tmp_path):
    path, pointer = _write_snapshot(tmp_path)
    with pytest.raises(ValueError, match="pointer keys differ"):
        protected.load_context(
            {**pointer, "unexpected": "ignored"}, _actions(), evidence_base_dir=tmp_path,
        )

    symlink = tmp_path / "snapshot-link.json"
    os.symlink(path, symlink)
    with pytest.raises(ValueError, match="symlink evidence path"):
        protected.load_context(
            {**pointer, "snapshot_path": symlink.name},
            _actions(), evidence_base_dir=tmp_path,
        )


def test_load_context_rejects_boolean_generation_and_wrong_action_group(tmp_path):
    snapshot = _snapshot()
    snapshot["payload"]["objects"]["cre_source_index"][0]["presence_generation"] = True
    _, pointer = _write_snapshot(tmp_path, snapshot)
    with pytest.raises(ValueError, match="protected context"):
        protected.load_context(pointer, _actions(), evidence_base_dir=tmp_path)

    _, pointer = _write_snapshot(tmp_path)
    actions = _actions()
    actions[1] = _actions()[1]
    actions[1]["external_id"] = EXTERNAL_IDS[1]
    actions[1]["listing_id"] = LISTING_IDS[1]
    actions[1]["source_url"] = URLS[0]
    actions[1]["before"]["presence_generation"] = 7
    actions[1]["before_hash"] = hashlib.sha256(
        b"false\x1factive\x1ffalse\x1ftrue\x1f7"
    ).hexdigest()
    actions[1]["desired_generation"] = 8
    actions[1]["event"]["presence_generation"] = 8
    with pytest.raises(ValueError, match="one protected current parent per shared URL"):
        protected.load_context(pointer, actions, evidence_base_dir=tmp_path)

    actions = _actions()
    actions[0]["before"]["listing_deleted"] = True
    with pytest.raises(ValueError, match="action before-state differs"):
        protected.load_context(pointer, actions, evidence_base_dir=tmp_path)
