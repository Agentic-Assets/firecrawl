"""Resolve property-field candidates while keeping per-field conflicts sticky."""


def merge_building_candidates(primary, secondary):
    conflicts = set(primary.get("_source_building_conflicts") or ()) | set(
        secondary.get("_source_building_conflicts") or ()
    )
    first = primary.get("source_field_building") or {}
    second = secondary.get("source_field_building") or {}
    merged = {}
    for field in sorted(set(first) | set(second)):
        if field in conflicts:
            continue
        left, right = first.get(field), second.get(field)
        if left is None or right is None:
            merged[field] = right if left is None else left
            continue
        identity = (
            "schema_version",
            "field",
            "source_key",
            "native_id",
            "url",
            "tenure",
            "value",
        )
        if any(left.get(key) != right.get(key) for key in identity):
            conflicts.add(field)
        elif left["observed_at"] == right["observed_at"] and left != right:
            conflicts.add(field)
        else:
            merged[field] = max(
                (left, right), key=lambda candidate: candidate["observed_at"]
            )
    return merged or None, sorted(conflicts)
