"""Validated Buildout shared-feed inventory bindings and field promotion."""

import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def _validate_feed(
    proof, definition, generation_id, start, collect_end, now, all_listings, tx_filter
):
    from cre_buildout_current_inventory import SORT, _digest, _owned_url
    from cre_current_inventory_admission import _time, require

    total, limit = proof.get("total"), proof.get("limit")
    require(
        type(total) is int
        and (total >= 0 if tx_filter is not None else total > 0)
        and type(limit) is int
        and limit > 0
    )
    pages = proof.get("pages")
    require(
        isinstance(pages, list)
        and len(pages) == max(1, (total + limit - 1) // limit)
        and len(pages) <= 1200
    )
    native = {}
    times = []
    for index, page in enumerate(pages):
        require(type(page.get("index")) is int and page["index"] == index)
        require(
            page.get("total") == total
            and type(page.get("total")) is int
            and page.get("limit") == limit
            and type(page.get("limit")) is int
        )
        endpoint = urlsplit(page.get("endpoint", ""))
        require(
            endpoint.scheme == "https"
            and endpoint.netloc == "buildout.com"
            and endpoint.path == f"/plugins/{definition['plugin']}/inventory.json"
            and not endpoint.fragment
        )
        require(
            parse_qs(endpoint.query, keep_blank_values=True, strict_parsing=True)
            == {"page": [str(index)], "q[s][]": [SORT]}
        )
        observed = _time(page.get("observedAt"))
        require(
            start <= observed <= collect_end
            and now - timedelta(hours=24) <= observed <= now + timedelta(minutes=5)
        )
        require(
            page.get("generationId") == generation_id
            and page.get("cacheDisposition") in {"live", "generation_cache"}
        )
        require(page.get("transport") in {"direct_json", "firecrawl_json"})
        _digest(page.get("parsedJsonSha256"))
        times.append(page["observedAt"])
        page_rows = page.get("rows")
        require(
            isinstance(page_rows, list)
            and len(page_rows) == min(limit, total - index * limit)
        )
        for item in page_rows:
            require(isinstance(item, dict))
            identity = item.get("id")
            require(
                isinstance(identity, str)
                and re.fullmatch(r"[1-9][0-9]{0,15}", identity)
                and int(identity) <= 9007199254740991
                and identity not in native
            )
            require(
                all(
                    type(item.get(k)) is bool
                    for k in ("sale", "closed", "underContract", "alsoForSaleOrLease")
                )
            )
            _owned_url(item.get("showLink"), definition["host"])
            native[identity] = (item, page)
    require(len(native) == total)
    expected = {
        identity: (item, page)
        for identity, (item, page) in native.items()
        if not item["closed"]
        and (tx_filter is None or item["sale"] == (tx_filter == "sale"))
    }
    listings = [
        r
        for r in all_listings
        if tx_filter is None or r.get("transactionMode") == tx_filter
    ]
    require(
        len(listings) == len(expected)
        and len({r.get("id") for r in listings}) == len(expected)
    )
    require({r.get("id") for r in listings} == set(expected))
    return listings, expected, pages, times, total


def validate_inventory(
    artifact, source, definition, generation_id, generation_started_at, *, now=None
):
    # Lazy imports avoid the ingest/admission import cycle.
    from cre_buildout_current_inventory import (
        SCHEMA,
        SORT,
        _url_identity,
    )
    from cre_current_inventory_admission import _time, require
    from cre_ingest import merge_rows, strict_full_source_scope, to_row

    scope = strict_full_source_scope(artifact, source)
    meta = artifact["runMeta"]
    fresh = meta.get("freshness") or {}
    start = _time(generation_started_at)
    now = now or datetime.now(UTC)
    require(
        fresh.get("generationId") == generation_id
        and fresh.get("requireFreshDetails") is True
    )
    require(_time(fresh.get("generationStartedAt")) == start)
    collect_start, collect_end = _time(meta["startedAt"]), _time(meta["finishedAt"])
    require(start <= collect_start <= collect_end <= now + timedelta(minutes=5))
    entries = artifact["sources"]
    require(
        len(entries) == 2
        and {r.get("transaction") for r in entries} == {"sale", "lease"}
    )
    by_tx = {r["transaction"]: r for r in entries}
    for tx, entry in by_tx.items():
        require(
            entry.get("sourceKey") == source
            and entry.get("supported") is True
            and not entry.get("error")
            and entry.get("truncated") is not True
        )
        proof = entry.get("currentInventoryProof")
        require(
            isinstance(proof, dict)
            and proof.get("schema") == SCHEMA
            and proof.get("kind") == "complete_paginated_feed"
        )
        require(
            proof.get("sourceKey")
            == (f"{source}-{tx}" if source == "franklin-street" else source)
            and proof.get("transaction") == tx
            and proof.get("generationId") == generation_id
        )
        require(
            proof.get("pluginKey")
            == (definition[tx] if source == "franklin-street" else definition)["plugin"]
            and proof.get("listingsPage")
            == (definition[tx] if source == "franklin-street" else definition)["page"]
            and proof.get("sort") == SORT
        )
    separate = source == "franklin-street"
    proof = by_tx["sale"]["currentInventoryProof"]
    other = by_tx["lease"]["currentInventoryProof"]
    if not separate:
        require(
            {k: v for k, v in proof.items() if k != "transaction"}
            == {k: v for k, v in other.items() if k != "transaction"}
        )
    listings = scope["listings"]
    expected = {}
    pages, times, totals = [], [], {}
    for tx in ("sale", "lease") if separate else (None,):
        selected = by_tx[tx]["currentInventoryProof"] if tx else proof
        feed_definition = definition[tx] if separate else definition
        feed_listings, native, feed_pages, feed_times, total = _validate_feed(
            selected,
            feed_definition,
            generation_id,
            start,
            collect_end,
            now,
            listings,
            tx,
        )
        for listing in feed_listings:
            key = (listing["transactionMode"], listing["id"])
            require(key not in expected)
            expected[key] = native[listing["id"]]
        pages.extend(feed_pages)
        times.extend(feed_times)
        if tx:
            totals[tx] = total
        else:
            totals = {"sale": total, "lease": total}
    require(expected)
    staged = defaultdict(list)
    stored_id_by_url = {}
    for listing in listings:
        item, page = expected[(listing["transactionMode"], listing["id"])]
        tx = "sale" if item["sale"] else "lease"
        require(
            listing.get("transactionMode") == tx
            and listing.get("transactionType")
            == (
                "Sale/Lease"
                if item["alsoForSaleOrLease"]
                else "Sale"
                if item["sale"]
                else "Lease"
            )
        )
        require(
            listing.get("url") == item["showLink"]
            and listing.get("canonicalUrl") == item["showLink"]
        )
        require(
            # collect.ts omits false scalars; raw receipt retains the explicit flag.
            listing.get("underContract", False) is item["underContract"]
            and listing.get("statusBadge")
            == ("under_contract" if item["underContract"] else None)
        )
        require(
            listing.get("preserveChildCollections") is True
            and listing.get("inventoryObservedAt") == page["observedAt"]
        )
        provenance = listing.get("freshnessProvenance") or {}
        require(isinstance(provenance, dict))
        require(
            provenance.get("generationId") == generation_id
            and provenance.get("cacheDisposition") == page["cacheDisposition"]
            and provenance.get("method") == "buildout_inventory_feed"
            and provenance.get("detailScope") == "authoritative_inventory_feed"
        )
        require(not listing.get("detailObservedAt"))
        transformed = to_row(listing, {}, meta["finishedAt"])
        require(transformed is not None)
        url_identity = _url_identity(listing["url"])
        external_id = transformed["external_id"]
        require(stored_id_by_url.get(url_identity, external_id) == external_id)
        stored_id_by_url[url_identity] = external_id
        staged[external_id].append((listing, transformed))
    staged_bindings = []
    for external, members in sorted(staged.items()):
        require(len(members) <= 2)
        if len(members) == 2:
            require({m[0]["transactionMode"] for m in members} == {"sale", "lease"})
            require(len({_url_identity(m[0]["url"]) for m in members}) == 1)
        merged = members[0][1]
        for _, transformed in members[1:]:
            merged = merge_rows(merged, transformed)
        staged_bindings.append(
            [external, merged["transaction_type"], sorted(m[0]["id"] for m in members)]
        )
    for tx, entry in by_tx.items():
        count = sum(r["transactionMode"] == tx for r in listings)
        require(
            type(entry.get("listingsCollected")) is int
            and entry["listingsCollected"] == count
            and type(entry.get("totalAvailableOnSource")) is int
            and entry["totalAvailableOnSource"] == totals[tx]
        )
    return {
        "listings": listings,
        "pages": pages,
        "times": times,
        "total": totals["sale"],
        "staged": staged,
        "staged_bindings": staged_bindings,
    }


def _core_definitions():
    """Read core coordinates from the same registry as the TypeScript producer."""
    from cre_current_inventory_admission import require

    rows = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "data"
            / "cre-buildout-core-feeds.json"
        ).read_text()
    )
    require(isinstance(rows, list) and len(rows) == 4)
    feeds = {}
    for row in rows:
        require(
            isinstance(row, list)
            and len(row) == 6
            and all(isinstance(v, str) and v for v in row)
        )
        source, company, plugin, page, host, key = row
        url = urlsplit(page)
        require(
            re.fullmatch(r"[a-f0-9]{40}", plugin)
            and url.scheme == "https"
            and url.netloc == host
            and not url.query
            and not url.fragment
        )
        require(key not in feeds)
        feeds[key] = {"company": company, "plugin": plugin, "page": page, "host": host}
        require(
            (
                source == "franklin-street"
                and key in {"franklin-street-sale", "franklin-street-lease"}
            )
            or (source in {"svn", "lee-associates"} and key == source)
        )
    require(
        set(feeds)
        == {"svn", "lee-associates", "franklin-street-sale", "franklin-street-lease"}
    )
    require(len({v["plugin"] for v in feeds.values()}) == 4)
    return {
        "svn": feeds["svn"],
        "lee-associates": feeds["lee-associates"],
        "franklin-street": {
            tx: feeds[f"franklin-street-{tx}"] for tx in ("sale", "lease")
        },
    }


def qualified_counts(artifact, artifact_sha256):
    """Return per-listing values and field holds after complete source proof.

    The caller has already validated strict freshness and hashed the exact bytes
    parsed into artifact. Invalid or unsupported proof yields no promotions.
    """
    from available_space_count import parse_buildout_available_space_count
    from cre_buildout_current_inventory import definitions
    from cre_current_inventory_admission import _time

    if (
        not isinstance(artifact_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
    ):
        return {}
    if not isinstance(artifact, dict) or not isinstance(artifact.get("runMeta"), dict):
        return {}
    fresh = artifact["runMeta"].get("freshness")
    if not isinstance(fresh, dict) or fresh.get("requireFreshDetails") is not True:
        return {}
    entries = artifact.get("sources")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) for entry in entries
    ):
        return {}
    for entry in entries:
        proof = entry.get("currentInventoryProof")
        if not isinstance(proof, dict) or not isinstance(proof.get("pages"), list):
            return {}
        if not all(isinstance(page, dict) for page in proof["pages"]):
            return {}
    if not all(isinstance(row.get("sourceKey"), str) for row in entries):
        return {}
    sources = {row["sourceKey"] for row in entries}
    if len(sources) != 1:
        return {}
    source = next(iter(sources))
    try:
        definition = definitions().get(source) or _core_definitions().get(source)
        if definition is None:
            return {}
        validated = validate_inventory(
            artifact,
            source,
            definition,
            fresh["generationId"],
            fresh["generationStartedAt"],
        )
    except (ValueError, TypeError, KeyError, OverflowError):
        return {}
    result = {}
    for listing in validated["listings"]:
        if listing.get("transactionMode") != "lease":
            continue
        obs = listing.get("buildoutFieldObservations")
        if obs is None:
            continue
        # An absent target label is no opinion. Once the target is present,
        # unknown or contradictory metadata must survive as a sticky hold.
        if isinstance(obs, dict) and "availableSpaceCount" not in obs:
            continue
        blocked = {"values": {}, "blocked": ["available_space_count"]}
        if (
            not isinstance(obs, dict)
            or obs.get("schema") != "buildout-field-observations-v1"
        ):
            result[id(listing)] = blocked
            continue
        field = obs.get("availableSpaceCount")
        if (
            isinstance(field, dict)
            and field.get("state") == "missing"
            and type(field.get("omittedCount")) is int
            and field["omittedCount"] == 0
            and type(obs.get("malformedAttributeCount")) is int
            and obs["malformedAttributeCount"] == 0
            and ("literals" not in field or field["literals"] == [])
            and "value" not in field
        ):
            continue
        if (
            not isinstance(field, dict)
            or field.get("state") != "observed"
            or type(field.get("omittedCount")) is not int
            or field["omittedCount"] != 0
            or type(obs.get("malformedAttributeCount")) is not int
            or obs["malformedAttributeCount"] != 0
        ):
            result[id(listing)] = blocked
            continue
        literals = field.get("literals")
        if (
            not isinstance(literals, list)
            or len(literals) != 1
            or not isinstance(literals[0], dict)
        ):
            result[id(listing)] = blocked
            continue
        item = literals[0]
        if (
            type(item.get("sourceOrdinal")) is not int
            or not 0 <= item["sourceOrdinal"] < 1000
        ):
            result[id(listing)] = blocked
            continue
        literal = item.get("literal")
        value = parse_buildout_available_space_count(literal)
        if (
            value is None
            or type(field.get("value")) is not int
            or field["value"] != value
        ):
            result[id(listing)] = blocked
            continue
        candidate = {
            "schema_version": "cre-source-field-observation/v1",
            "field": "available_space_count",
            "source_label": "# of Spaces",
            "source_ordinal": item["sourceOrdinal"],
            "original_observed_at": listing["inventoryObservedAt"],
            "source_key": source,
            "native_id": listing["id"],
            "url": listing["url"],
            "tenure": "lease",
            "observed_at": _time(listing["inventoryObservedAt"]).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "generation_id": fresh["generationId"],
            "literal": literal,
            "artifact_sha256": artifact_sha256,
            "value": value,
        }
        result[id(listing)] = {
            "values": {"available_space_count": candidate},
            "blocked": [],
        }
    return result


def rebind_count_decision(original, staged, decision):
    """Carry a qualified count only while its staged witnesses remain intact."""
    witnesses = (
        "sourceKey", "id", "url", "transactionMode", "freshnessProvenance",
        "canonicalUrl", "transactionType", "underContract", "statusBadge",
        "preserveChildCollections", "inventoryObservedAt", "detailObservedAt",
        "buildoutFieldObservations",
    )
    if any(original.get(key) != staged.get(key) for key in witnesses):
        return {"values": {}, "blocked": ["available_space_count"]}
    return decision


def classify_prior_inventory_presence(validated, prior_bindings, source):
    """Classify source-qualified prior native IDs against a validated full feed.

    This is an observation-only staging step. The caller must independently bind
    the validated proof to the exact generation, target and owner, authenticate
    prior bindings and serialize/recheck them in the ingest transaction. No
    result here authorizes a database write or a lifecycle/status change.
    """
    if not isinstance(source, str) or not source or not isinstance(validated, dict):
        raise ValueError("invalid inventory scope")
    pages = validated.get("pages")
    if not isinstance(pages, list) or not pages:
        raise ValueError("missing validated inventory pages")
    observed = set()
    for page in pages:
        rows = page.get("rows") if isinstance(page, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid validated inventory rows")
        for row in rows:
            native = row.get("id") if isinstance(row, dict) else None
            if (not isinstance(native, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", native)
                    or int(native) > 9007199254740991):
                raise ValueError("invalid validated inventory identity")
            observed.add(native)
    if not isinstance(prior_bindings, list):
        raise ValueError("invalid prior bindings")
    claims = defaultdict(int)
    external_claims = defaultdict(int)
    for prior in prior_bindings:
        if not isinstance(prior, dict):
            continue
        ids = prior.get("native_ids")
        if isinstance(prior.get("external_id"), str):
            external_claims[prior["external_id"]] += 1
        if prior.get("source_key") == source and isinstance(ids, list):
            for native in set(item for item in ids if isinstance(item, str)):
                claims[native] += 1
    result = {"not_observed": [], "still_observed": [], "held": []}
    for prior in prior_bindings:
        if not isinstance(prior, dict):
            result["held"].append(prior)
            continue
        identity = prior.get("external_id")
        ids = prior.get("native_ids")
        qualified = (
            prior.get("source_key") == source
            and isinstance(identity, str) and bool(identity)
            and external_claims[identity] == 1
            and prior.get("observation_present") is True
            and isinstance(ids, list) and bool(ids)
            and all(isinstance(item, str) and re.fullmatch(r"[1-9][0-9]{0,15}", item)
                    and int(item) <= 9007199254740991 and claims[item] == 1 for item in ids)
            and len(ids) == len(set(ids))
        )
        if not qualified:
            result["held"].append(prior)
        elif any(native in observed for native in ids):
            result["still_observed"].append(prior)
        else:
            result["not_observed"].append(prior)
    return result


def prepare_inventory_observation_scope(
    raw_artifact, manifest, source, *, current_code_sha, current_git_dirty, current_target_sha256
):
    """Bind a Buildout full-feed proof to its original checkpoint inputs.

    This validates inputs only. It does not authenticate the checkpoint owner,
    authorize an ingest, or establish a database watermark or readback.
    """
    import hashlib

    from cre_buildout_current_inventory import definitions
    from cre_current_inventory_admission import require

    require(isinstance(raw_artifact, bytes))
    require(isinstance(manifest, dict) and isinstance(source, str))
    config = manifest.get("config") or {}
    require(config.get("additive") is True and config.get("max_items") == 0)
    require(tuple(config.get("transactions") or ()) == ("sale", "lease"))
    require(config.get("mark_missing") is False and config.get("status_activation") is False)
    require(manifest.get("collector_git_dirty") is False and current_git_dirty is False)
    require(isinstance(current_code_sha, str) and re.fullmatch(r"[a-f0-9]{40}", current_code_sha))
    require(manifest.get("collector_git_sha") == current_code_sha)
    target = (manifest.get("preflight") or {}).get("database_target") or {}
    require(target.get("algorithm") == "sha256")
    require(isinstance(target.get("value"), str) and re.fullmatch(r"[a-f0-9]{64}", target["value"]))
    require(current_target_sha256 == target["value"])
    info = ((manifest.get("sources") or {}).get(source) or {}).get("artifact") or {}
    digest = hashlib.sha256(raw_artifact).hexdigest()
    require(info.get("sha256") == digest and type(info.get("bytes")) is int and info["bytes"] == len(raw_artifact))
    registry = definitions()
    require(source in registry)
    artifact = json.loads(raw_artifact)
    validated = validate_inventory(
        artifact, source, registry[source], manifest["run_id"], manifest["started_at"]
    )
    # Include every provider row, even closed records, to avoid equating an
    # inactive but visible source observation with absence from the feed.
    native = sorted({row["id"] for page in validated["pages"] for row in page["rows"]})
    return {
        "source_key": source,
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "collector_git_sha": current_code_sha,
        "database_target_sha256": target["value"],
        "artifact_sha256": digest,
        "observed_at": artifact["runMeta"]["finishedAt"],
        "native_ids": native,
        "staged_external_ids": sorted(validated["staged"]),
    }


def qualify_prior_inventory_binding(source, listing, index):
    """Conservatively bind a prior canonical row to source-native IDs.

    These are candidate preimages only: the actual listing and index rows must
    be locked and compared again inside the transaction that writes an event.
    Unsupported or conflicting historical shapes return None.
    """
    if not isinstance(source, str) or not isinstance(listing, dict) or not isinstance(index, dict):
        return None
    external_id = listing.get("external_id")
    source_url = listing.get("source_url")
    if (not isinstance(listing.get("id"), str) or not listing["id"]
            or not isinstance(listing.get("brokerage_id"), str) or not listing["brokerage_id"]
            or not isinstance(index.get("last_enumerated_at"), str) or not index["last_enumerated_at"]
            or not isinstance(external_id, str) or not external_id
            or not isinstance(source_url, str) or not source_url
            or listing.get("brokerage_id") != index.get("brokerage_id")
            or external_id != index.get("external_id")
            or index.get("source_key") != source or index.get("url") != source_url
            or index.get("observation_present") is not True
            or type(index.get("presence_generation")) is not int
            or index["presence_generation"] < 0):
        return None
    raw = listing.get("raw_data")
    if not isinstance(raw, dict):
        return None
    if "primary" in raw or "secondary_pass" in raw:
        if set(raw) != {"primary", "secondary_pass"}:
            return None
        members = [raw["primary"], raw["secondary_pass"]]
    else:
        members = [raw]
    if not all(isinstance(member, dict) and member.get("sourceKey") == source for member in members):
        return None
    ids = []
    urls = []
    for member in members:
        value = member.get("id")
        if type(value) is int:
            value = str(value)
        if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,15}", value)
                or int(value) > 9007199254740991):
            return None
        url = member.get("url")
        if not isinstance(url, str) or not url:
            return None
        ids.append(value)
        urls.append(url)
    if source_url not in urls or len(ids) != len(set(ids)):
        return None
    return {
        "external_id": external_id, "source_key": source,
        "native_ids": ids, "observation_present": True,
        "listing_id": listing.get("id"), "brokerage_id": listing.get("brokerage_id"),
        "source_url": source_url, "raw_data": raw,
        "presence_generation": index["presence_generation"],
        "last_enumerated_at": index.get("last_enumerated_at"),
    }
