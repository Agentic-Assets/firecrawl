"""Derive source-scoped NAI inventory recovery without granting write authority.

The coordinator and source gate must bind the original bytes, owner, source
attempt, code and target before any candidate derived here can be admitted.
"""
import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal

from cre_nai_post_partial import _encode_json, _timestamp, partition_posts

SCHEMA = "nai-post-inventory-recovery/v1"
_ACCESS = re.compile(r"(?:\b(?:401|403|429)\b|unauthori[sz]ed|forbidden|rate.?limit|retry.after|challenge|captcha)", re.I)


def derive(original: bytes, generation: str, generation_started: str, attempt_started: str,
           attempt_finished: str, *, now, max_age_hours=24):
    """Reproduce a partial publication partition from one exact source body."""
    data = json.loads(original)
    exact = json.loads(original, parse_float=Decimal)
    if not isinstance(data, dict) or data.get("schema") is not None:
        raise ValueError("NAI recovery original schema invalid")
    meta = data.get("runMeta")
    freshness = meta.get("freshness") if isinstance(meta, dict) else None
    if (not isinstance(meta, dict) or meta.get("mode") != "full"
            or meta.get("transactions") != ["sale", "lease"] or meta.get("maxItemsPerSource") is not None
            or not isinstance(freshness, dict) or freshness.get("generationId") != generation
            or _timestamp(freshness.get("generationStartedAt")) != _timestamp(generation_started)):
        raise ValueError("NAI recovery source scope differs")
    start, finish = _timestamp(meta.get("startedAt")), _timestamp(meta.get("finishedAt"))
    if not (_timestamp(generation_started) <= _timestamp(attempt_started) <= start <= finish
            <= _timestamp(attempt_finished) and finish <= now + timedelta(minutes=5)):
        raise ValueError("NAI recovery source clocks differ")
    rows, entries = data.get("listings"), data.get("sources")
    if (not isinstance(rows, list) or not rows or data.get("totalListings") != len(rows)
            or not isinstance(data.get("brokers"), list) or not isinstance(entries, list) or len(entries) != 2):
        raise ValueError("NAI recovery source shape differs")
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("NAI recovery source entry invalid")
        tx = entry.get("transaction")
        if (entry.get("sourceKey") != "nai-global" or tx not in {"sale", "lease"} or tx in seen
                or entry.get("supported") is not True or entry.get("truncated") is not False or entry.get("error")
                or type(entry.get("listingsCollected")) is not int or entry["listingsCollected"] < 0):
            raise ValueError("NAI recovery source pass is not settled complete")
        seen.add(tx)
    if sum(e["listingsCollected"] for e in entries) != len(rows):
        raise ValueError("NAI recovery source count differs")
    inventory_only = False
    for row in rows:
        if not isinstance(row, dict) or row.get("sourceKey") != "nai-global":
            raise ValueError("NAI recovery source row scope differs")
        detail_error = row.get("detailError")
        if detail_error and (not isinstance(detail_error, str) or _ACCESS.search(detail_error)):
            raise ValueError("NAI recovery access outcome is not qualified")
        provenance = row.get("freshnessProvenance")
        if isinstance(provenance, dict) and provenance.get("detailScope") == "inventory_only":
            inventory_only = True
    if not inventory_only:
        raise ValueError("NAI recovery lacks an inventory-only observation")
    partition = partition_posts(rows, generation, capture_start=max(start, now - timedelta(hours=max_age_hours)),
                                capture_finish=finish, exact_listings=exact["listings"])
    if not partition["staged"]:
        raise ValueError("NAI recovery has no identified source posts")
    held_indices = {item["index"] for item in partition["held"]}
    selected = [row for index, row in enumerate(exact["listings"]) if index not in held_indices]
    candidate = {**exact, "schema": SCHEMA, "listings": selected, "totalListings": len(selected),
                 "naiRecovery": {"originalSha256": hashlib.sha256(original).hexdigest(),
                                 "originalCount": len(rows), "sourceHeld": partition["held"]}}
    return (_encode_json(candidate) + "\n").encode("ascii"), partition
