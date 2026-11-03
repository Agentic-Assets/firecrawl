"""Offline contracts for the Pyramid verified-subset transaction guard."""
import copy
import hashlib
import json

import pytest

from cre_pyramid_subset_sql import guard_sql


def claims():
    members = [{"native_id": n, "external_id": f"listings/property-{n}",
                "source_url": f"https://www.pyramidbrokerage.com/listings/property-{n}/",
                "transaction": "lease" if n % 2 else "sale", "emitted": n <= 99}
               for n in range(1, 103)]
    digest = hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": "pyramid-verified-subset-v1", "attempt_id": "a" * 32,
            "binding": {"scope": "pyramid_verified_subset_v1", "artifact_sha256": "b" * 64,
                        "inventory_count": 102, "emitted_count": 99, "held_count": 3,
                        "members_sha256": digest}, "members": members}


def render(value):
    return guard_sql(value, mark_missing=False, status_activation=False, inventory_only=False)


def test_bound_partition_sql_checks_all_members_before_parent_upsert():
    sql = render(claims())
    assert "credeals.cre_source_baseline" in sql
    assert "WHERE emitted" in sql
    assert "staging does not equal emitted identities" in sql
    assert "historical identity contradiction including held members" in sql
    assert "would revive lifecycle or presence" in sql
    assert "si.source_key IS DISTINCT FROM 'pyramid-brokerage'" in sql
    assert "transaction_type" in sql and "wordpressRecord,id" in sql
    assert "UPDATE credeals." not in sql and "DELETE FROM credeals." not in sql


@pytest.mark.parametrize("kind", ["bad_sha", "changed_member", "duplicate_post", "duplicate_path", "bad_tenure", "held_count", "no_holds"])
def test_refuses_unbound_or_ambiguous_partition(kind):
    value = copy.deepcopy(claims())
    if kind == "bad_sha": value["binding"]["artifact_sha256"] = "invalid"
    if kind == "changed_member": value["members"][0]["transaction"] = "sale"
    if kind == "duplicate_post": value["members"][1]["native_id"] = value["members"][0]["native_id"]
    if kind == "duplicate_path": value["members"][1]["source_url"] = value["members"][0]["source_url"]
    if kind == "bad_tenure": value["members"][0]["transaction"] = "unknown"
    if kind == "held_count": value["binding"]["held_count"] = 2
    if kind == "no_holds": value["binding"]["held_count"] = 0
    with pytest.raises((ValueError, TypeError)):
        render(value)


@pytest.mark.parametrize("flag", ["mark_missing", "status_activation", "inventory_only"])
def test_refuses_lifecycle_and_inventory_only_scope(flag):
    options = {"mark_missing": False, "status_activation": False, "inventory_only": False}
    options[flag] = True
    with pytest.raises(ValueError):
        guard_sql(claims(), **options)


def test_refuses_zero_emitted_or_under_floor_inventory():
    for empty, under_floor in [(True, False), (False, True)]:
        value = claims()
        if empty:
            for member in value["members"]: member["emitted"] = False
        if under_floor: value["members"] = value["members"][:99]
        value["binding"].update(inventory_count=len(value["members"]),
            emitted_count=sum(m["emitted"] for m in value["members"]),
            held_count=sum(not m["emitted"] for m in value["members"]),
            members_sha256=hashlib.sha256(json.dumps(value["members"], sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        with pytest.raises(ValueError): render(value)
