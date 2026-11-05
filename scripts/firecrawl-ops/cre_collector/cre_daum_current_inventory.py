"""Pure DAUM complete strict-detail absent-baseline additive admission."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cre_current_inventory_admission import (
    SCOPE,
    VERDICT,
    _time,
    admission_configuration,
    configuration,
    require,
    subset_configuration,
    validate_report_scope,
)
from cre_gate import count_artifacts
from cre_ingest import (
    merge_rows,
    strict_full_source_scope,
    to_row,
    validate_strict_artifact_freshness,
)
from cre_source_policy import load_source_policy

SOURCE = "daum-commercial"
SCHEMA = "daum-strict-detail-v1"
SUBSET_SCHEMA = "daum-verified-subset-v1"
SUBSET_SCOPE = "daum_verified_subset_v1"
SUBSET_SCHEMA_V2 = "daum-verified-subset-v2"
SUBSET_SCOPE_V2 = "daum_verified_subset_v2"
SUBSET_POLICY_V2 = "tenure-and-empty-title-v2"
SUBSET_VERDICT = "ok_additive_verified_subset"
SUBSET_REASON = "verified subset admitted additively with explicit tenure holds; whole source remains unqualified"
BASE = "https://daumcommercial.com/property-search/"
REASON = (
    "complete current inventory admitted additively; historical baseline remains absent"
)


def _sha(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))


def _path_key(value):
    require(
        isinstance(value, str)
        and "\\" not in value
        and not any(c.isspace() or ord(c) < 32 for c in value)
    )
    url = urlsplit(value)
    require(
        url.scheme == "https"
        and url.netloc == "daumcommercial.com"
        and not url.query
        and not url.fragment
        and re.fullmatch(r"/property/[^/]+/", url.path)
    )
    require(
        url.path.split("/")[2].lower()
        not in {".", "..", "%2e", ".%2e", "%2e.", "%2e%2e"}
    )
    return url.path.strip("/")


def _post(value):
    require(
        isinstance(value, str)
        and re.fullmatch(r"[1-9][0-9]{0,15}", value)
        and int(value) <= 9007199254740991
    )


TENURES = {"sale", "lease", "sale_or_lease", "unknown"}


def _held_inventory_observations(proof, held, native):
    """Check optional card audit against the already-derived held partition."""
    if "heldInventoryObservations" not in proof:
        return
    observations = proof["heldInventoryObservations"]
    require(isinstance(observations, list) and len(observations) == len(held))
    nullable_text = {
        "tenureText", "city", "state", "postalCode", "sizeText", "assetType",
        "salePriceText", "leaseRateText", "imageUrl",
    }
    numeric = {"spaceCount", "latitude", "longitude"}
    fields = nullable_text | numeric | {"url", "title", "tenure", "brokerNames"}
    for observation, (post, _reason) in zip(observations, held, strict=True):
        require(isinstance(observation, dict) and set(observation) == {"postId", "inventory"})
        require(observation["postId"] == post)
        inventory = observation["inventory"]
        require(isinstance(inventory, dict) and set(inventory) == fields)
        require(all(isinstance(inventory[key], str) for key in ("url", "title", "tenure")))
        require(bool(inventory["title"].strip()))
        require(all(inventory[key] is None or isinstance(inventory[key], str)
                    for key in nullable_text))
        for key in numeric:
            value = inventory[key]
            if value is not None:
                require(type(value) in {int, float})
                try:
                    finite = math.isfinite(value)
                except OverflowError:
                    finite = False
                require(finite)
        require(isinstance(inventory["brokerNames"], list)
                and all(isinstance(name, str) for name in inventory["brokerNames"]))
        url, _key, tenure, raw, *_ = native[post]
        require(inventory["url"] == url and inventory["tenure"] == tenure
                and inventory["tenureText"] == raw
                and inventory["tenure"] == _tenure(inventory["tenureText"]))


def _held_detail_observations(proof, held, native, details, v2, empty_titles):
    """Check optional parsed-detail audit against the already-derived held partition."""
    if "heldDetailObservations" not in proof:
        return
    observations = proof["heldDetailObservations"]
    require(isinstance(observations, list) and len(observations) == len(held))
    full_fields = {
        "postId", "observedCanonicalUrl", "facts", "title", "tenure", "tenureText",
        "city", "state", "postalCode", "salePriceText", "leaseRateText", "sizeText",
        "description", "highlights", "brochures", "photos", "contacts",
    }
    marker_fields = {"postId", "observedCanonicalUrl", "tenure", "tenureText",
                     "emptyRequiredTitle"}
    nullable_text = {
        "tenureText", "city", "state", "postalCode", "salePriceText",
        "leaseRateText", "sizeText", "description",
    }
    contact_fields = {
        "name", "email", "phone", "office", "profileUrl", "avatarUrl", "license",
    }
    for observation, (post, reason) in zip(observations, held, strict=True):
        require(isinstance(observation, dict) and set(observation) == {"postId", "detail"})
        require(observation["postId"] == post)
        detail = observation["detail"]
        require(isinstance(detail, dict))
        require(type(detail.get("postId")) is int and detail["postId"] == int(post))
        require(detail.get("observedCanonicalUrl") == native[post][0])
        require(detail.get("tenure") == native[post][4]
                and detail.get("tenureText") == native[post][5])
        optional_fields = {"availableSpaceObservations"} if "availableSpaceObservations" in detail else set()
        if optional_fields:
            spaces = detail["availableSpaceObservations"]
            require(isinstance(spaces, list))
            require(all(isinstance(space, dict) and set(space) == {"html", "text"}
                        and isinstance(space["html"], str) and isinstance(space["text"], str)
                        for space in spaces))
        if reason == "empty_required_property_title":
            require(v2 and post in empty_titles and set(detail) == marker_fields | optional_fields)
            require(detail["emptyRequiredTitle"] is True)
            continue
        require(reason == "unknown_inventory_and_detail_tenure")
        require(set(detail) == full_fields | optional_fields and post not in empty_titles)
        require(detail["tenure"] in TENURES)
        require(isinstance(detail["title"], str) and bool(detail["title"].strip()))
        require(isinstance(detail["facts"], dict)
                and all(isinstance(key, str) and isinstance(value, str)
                        for key, value in detail["facts"].items()))
        for key in nullable_text:
            require(detail[key] is None or isinstance(detail[key], str))
        require(isinstance(detail["highlights"], list)
                and all(isinstance(value, str) for value in detail["highlights"]))
        require(isinstance(detail["photos"], list)
                and all(isinstance(value, str) for value in detail["photos"]))
        require(isinstance(detail["brochures"], list))
        for brochure in detail["brochures"]:
            require(isinstance(brochure, dict) and set(brochure) == {"name", "url"})
            require(brochure["name"] is None or isinstance(brochure["name"], str))
            require(isinstance(brochure["url"], str))
        require(isinstance(detail["contacts"], list))
        for contact in detail["contacts"]:
            require(isinstance(contact, dict) and set(contact) == contact_fields)
            require(all(contact[key] is None or isinstance(contact[key], str)
                        for key in contact_fields))
        require(details[post][0] == detail["observedCanonicalUrl"])


def _prune_projection(value):
    """Match collector prune for the fixed unknown-tenure projection."""
    if value is None or value is False or value == "":
        return None
    if isinstance(value, list):
        result = [_prune_projection(item) for item in value]
        result = [item for item in result if item is not None]
        return result or None
    if isinstance(value, dict):
        result = {key: _prune_projection(item) for key, item in value.items()}
        result = {key: item for key, item in result.items() if item is not None}
        return result or None
    return value


def _unknown_projection(inventory, detail, *, generation, inventory_at, detail_at, body_hash):
    """Reconstruct the only writable unknown-tenure fields from bound evidence."""
    facts = detail["facts"]
    def choose(value, fallback):
        return fallback if value is None else value

    contacts = detail["contacts"] or [{"name": name} for name in inventory["brokerNames"]]
    photos = list(dict.fromkeys(item for item in [*detail["photos"], inventory["imageUrl"]] if item))
    return _prune_projection({
        "sourceKey": SOURCE, "sourceCompany": "DAUM Commercial Real Estate Services",
        "transactionMode": "sale",
        "id": _path_key(inventory["url"]), "daumPostId": str(detail["postId"]),
        "name": detail["title"], "street": detail["title"],
        "assetType": choose(facts.get("Property Type"), inventory["assetType"]),
        "propertySubtype": facts.get("Primary Use"),
        "description": detail["description"], "highlights": detail["highlights"],
        "daumAvailableSpaceObservations": detail.get("availableSpaceObservations"),
        "city": choose(detail["city"], inventory["city"]),
        "state": choose(detail["state"], inventory["state"]),
        "postalCode": choose(detail["postalCode"], inventory["postalCode"]),
        "latitude": inventory["latitude"], "longitude": inventory["longitude"],
        "sizeText": choose(detail["sizeText"], inventory["sizeText"]), "zoning": facts.get("Zoning"),
        "contactsDetailed": contacts, "brochures": detail["brochures"], "photos": photos,
        "url": inventory["url"], "canonicalUrl": inventory["url"],
        "inventoryObservedAt": inventory_at, "detailObservedAt": detail_at,
        "freshnessProvenance": {
            "detailScope": "detail_page", "generationId": generation,
            "method": "daum_direct_detail_html", "cacheDisposition": "live",
            "identityMethod": "wordpress_shortlink_post_id",
        },
        "daumTenureEvidence": {
            "inventory": inventory["tenure"], "inventoryText": inventory["tenureText"],
            "detail": detail["tenure"], "detailText": detail["tenureText"], "resolved": "unknown",
        },
        "daumObservedCanonicalUrl": detail["observedCanonicalUrl"],
        "daumUnknownTenure": True, "preserveChildCollections": True,
        "detailObservedWithChildPreservation": True, "daumDetailResponseSha256": body_hash,
    })


def _validate_unknown_projection(listing, inventory, detail, *, generation, inventory_at, detail_at, body_hash):
    try:
        evidence = json.loads(listing.get("daumUnknownEvidence"))
    except (TypeError, ValueError):
        require(False)
    require(evidence == {"inventory": inventory, "detail": detail})
    require(
        _sha({key: value for key, value in listing.items() if key != "daumUnknownEvidence"})
        == _sha(_unknown_projection(
            inventory, detail, generation=generation, inventory_at=inventory_at,
            detail_at=detail_at, body_hash=body_hash,
        ))
    )


def _tenure(raw):
    require(raw is None or isinstance(raw, str))
    value = " ".join((raw or "").split()).lower()
    if not value:
        return "unknown"
    if value in {"lease or sale", "sale or lease"}:
        return "sale_or_lease"
    if value in {"lease", "sublease"}:
        return "lease"
    if value in {"sale", "sale- user", "sale- investment"}:
        return "sale"
    raise ValueError("Unrecognized DAUM primary tenure")


def _resolved(inventory, detail):
    require(inventory in TENURES and detail in TENURES)
    if inventory == "unknown":
        return detail
    require(detail in {"unknown", inventory})
    return inventory


def _pass(snapshot, previous, observed, total):
    require(isinstance(snapshot, dict) and type(snapshot.get("index")) is int)
    require(snapshot["index"] >= 1 and snapshot.get("total") == total)
    pages = snapshot["pages"]
    expected_pages = (total + 89) // 90
    require(isinstance(pages, list) and len(pages) == expected_pages)
    members = {}
    for index, page in enumerate(pages, 1):
        endpoint = BASE if index == 1 else BASE + f"page/{index}/"
        require(isinstance(page, dict) and page.get("endpoint") == endpoint)
        require(page.get("method") == "POST")
        require(
            type(page.get("reportedTotal")) is int and page["reportedTotal"] == total
        )
        require(
            type(page.get("reportedPages")) is int
            and page["reportedPages"] == expected_pages
        )
        _digest(page.get("responseTextSha256"))
        clock = _time(page.get("observedAt"))
        require(previous <= clock <= observed)
        previous = clock
        rows = page["members"]
        require(
            isinstance(rows, list) and len(rows) == min(90, total - (index - 1) * 90)
        )
        for member in rows:
            require(isinstance(member, list) and len(member) == 3)
            url, tenure, raw = member
            _path_key(url)
            require(url not in members and tenure == _tenure(raw))
            members[url] = (tenure, raw)
    require(len(members) == total)
    return members, previous


def subset_source_scope(data):
    """Exact emitted artifact shape, without granting full-source authority."""
    require(isinstance(data, dict))
    meta = data.get("runMeta")
    require(isinstance(meta, dict))
    require(
        meta.get("mode") == "full"
        and meta.get("transactions") == ["sale", "lease"]
        and meta.get("maxItemsPerSource") is None
    )
    require(_time(meta.get("startedAt")) <= _time(meta.get("finishedAt")))
    entries, listings = data.get("sources"), data.get("listings")
    require(
        isinstance(entries, list)
        and len(entries) == 2
        and isinstance(listings, list)
        and type(data.get("totalListings")) is int
        and data["totalListings"] == len(listings)
    )
    require(
        all(
            isinstance(entry, dict) and entry.get("sourceKey") == SOURCE
            for entry in entries
        )
    )
    require(sorted(entry.get("transaction") for entry in entries) == ["lease", "sale"])
    for entry in entries:
        proof = entry.get("currentInventoryProof")
        require(
            isinstance(proof, dict)
            and proof.get("schema") in {SUBSET_SCHEMA, SUBSET_SCHEMA_V2}
            and proof.get("kind") == "verified_subset_with_holds"
            and isinstance(proof.get("held"), list)
        )
        require(
            entry.get("supported") is True
            and entry.get("error") is None
            and entry.get("truncated") is bool(proof["held"])
            and type(entry.get("listingsCollected")) is int
            and type(entry.get("totalAvailableOnSource")) is int
        )
        require(
            all(
                isinstance(row, dict)
                and row.get("sourceKey") == SOURCE
                and row.get("transactionMode") in {"sale", "lease"}
                for row in listings
            )
        )
        require(
            sum(row["transactionMode"] == entry["transaction"] for row in listings)
            == entry["listingsCollected"]
        )
    return {"entries": entries, "listings": listings}


def checkpoint_retained_unknown_ids(data):
    """Bind only child-preserving rows; full subset admission remains later."""
    scope = subset_source_scope(data)
    proofs = {
        entry["transaction"]: entry["currentInventoryProof"]
        for entry in scope["entries"]
    }
    sale, lease = proofs["sale"], proofs["lease"]
    from cre_daum_fallback_partition import staged_fallback_members

    fallback_ids = {row["native_id"] for row in staged_fallback_members(data)} if sale.get("titleFallbackObservations") else set()
    retained = sale.get("retainedUnknownTenure", [])
    require(isinstance(retained, list))
    if not retained:
        require(
            all(
                (not row.get("preserveChildCollections") or row.get("daumPostId") in fallback_ids)
                and not row.get("daumUnknownTenure")
                for row in scope["listings"]
            )
        )
        return frozenset()
    require(
        {key: value for key, value in sale.items() if key != "transaction"}
        == {key: value for key, value in lease.items() if key != "transaction"}
        and sale.get("transaction") == "sale"
        and lease.get("transaction") == "lease"
        and sale.get("sourceKey") == SOURCE
        and sale.get("generationId")
        == (data["runMeta"].get("freshness") or {}).get("generationId")
        and len(retained) == len(set(retained))
    )
    held = sale.get("held")
    require(
        isinstance(held, list)
        and all(isinstance(row, list) and len(row) == 2 for row in held)
    )
    require(
        retained
        == [
            post
            for post, reason in held
            if reason == "unknown_inventory_and_detail_tenure"
        ]
    )
    cohort, receipts = sale.get("cohort"), sale.get("details")
    require(isinstance(cohort, list) and isinstance(receipts, list))
    native, details = {}, {}
    for row in cohort:
        if not isinstance(row, list) or not row or row[0] not in retained:
            continue  # The full admission verifies non-retained cohort members.
        require(len(row) == 8)
        post, url, key, inv, inv_raw, detail, detail_raw, resolved = row
        _post(post)
        require(post not in native and key == _path_key(url))
        require(
            inv == detail == resolved == "unknown"
            and inv == _tenure(inv_raw)
            and detail == _tenure(detail_raw)
        )
        native[post] = (url, key, inv, inv_raw, detail, detail_raw, resolved)
    for row in receipts:
        if not isinstance(row, list) or not row or row[0] not in retained:
            continue
        require(len(row) == 4)
        post, canonical, clock, body_hash = row
        require(post in native and post not in details and canonical == native[post][0])
        _time(clock)
        _digest(body_hash)
        details[post] = (canonical, clock, body_hash)
    require(set(native) == set(details) == set(retained))
    # The same validators used by source admission check the retained evidence.
    retained_held = [[post, "unknown_inventory_and_detail_tenure"] for post in retained]
    observed_proof = dict(sale)
    for name in ("heldInventoryObservations", "heldDetailObservations"):
        values = sale.get(name)
        require(isinstance(values, list))
        observed_proof[name] = [
            item
            for item in values
            if isinstance(item, dict) and item.get("postId") in retained
        ]
    _held_inventory_observations(observed_proof, retained_held, native)
    _held_detail_observations(
        observed_proof, retained_held, native, details, False, set()
    )
    inventories = {
        item["postId"]: item["inventory"]
        for item in observed_proof["heldInventoryObservations"]
    }
    observed_details = {
        item["postId"]: item["detail"]
        for item in observed_proof["heldDetailObservations"]
    }
    seen = set()
    for row in scope["listings"]:
        post = row.get("daumPostId")
        if post not in retained:
            require(
                (not row.get("preserveChildCollections") or row.get("daumPostId") in fallback_ids)
                and not row.get("daumUnknownTenure")
            )
            continue
        require(post not in seen and row.get("transactionMode") == "sale")
        seen.add(post)
        _validate_unknown_projection(
            row,
            inventories[post],
            observed_details[post],
            generation=sale["generationId"],
            inventory_at=sale["observedAt"],
            detail_at=details[post][1],
            body_hash=details[post][2],
        )
    require(seen == set(retained))
    return frozenset(retained)

def admit(artifact_path, manifest, report, **kwargs):
    return _admit(artifact_path, manifest, report, subset=False, **kwargs)


def admit_subset(artifact_path, manifest, report, **kwargs):
    return _admit(artifact_path, manifest, report, subset=True, **kwargs)


def _admit(
    artifact_path: Path,
    manifest: dict,
    report: dict,
    *,
    current_code_sha: str,
    current_git_dirty: bool,
    phase: str,
    source=SOURCE,
    subset=False,
) -> dict:
    require(source == SOURCE and phase in {"source", "aggregate"})
    config = manifest["config"]
    require(
        (
            config.get("pipeline") is True
            and SOURCE in config["sources"]
            or config["sources"] == [SOURCE]
        )
        and config["transactions"] == ["sale", "lease"]
    )
    expected_configuration = configuration(SOURCE)
    selected = admission_configuration(config, SOURCE)
    v2 = subset and isinstance(selected, dict) and selected.get("admission_scope") == SUBSET_SCOPE_V2
    if subset:
        expected_configuration = subset_configuration(SUBSET_POLICY_V2 if v2 else "tenure-only-v1")
    require(selected == expected_configuration)
    schema, scope_kind, verdict, reason = (
        (SUBSET_SCHEMA, SUBSET_SCOPE, SUBSET_VERDICT, SUBSET_REASON)
        if subset
        else (SCHEMA, SCOPE, VERDICT, REASON)
    )
    if v2:
        schema, scope_kind = SUBSET_SCHEMA_V2, SUBSET_SCOPE_V2
        reason = "verified subset admitted additively with explicit incomplete-detail holds; whole source remains unqualified"
    require(
        config.get("additive") is True
        and config.get("mark_missing") is False
        and config.get("status_activation") is False
        and config.get("max_items") == 0
    )
    require(manifest.get("collector_git_dirty") is False and current_git_dirty is False)
    require(
        re.fullmatch(r"[a-f0-9]{40}", current_code_sha)
        and manifest["collector_git_sha"] == current_code_sha
    )
    policy = load_source_policy()[SOURCE]
    require(
        [
            policy[k]
            for k in (
                "evidence_class",
                "canonical_claim",
                "detail_claim",
                "child_contract",
                "inventory_only_namespace",
            )
        ]
        == [
            "strict_detail",
            "canonical_listing",
            "current_strict_detail",
            "replace_from_fresh_detail",
            None,
        ]
    )
    target = manifest["preflight"]["database_target"]
    require(target.get("algorithm") == "sha256")
    _digest(target.get("value"))
    info = manifest["sources"][SOURCE]["artifact"]
    require(not artifact_path.is_symlink() and artifact_path.is_file())
    raw = artifact_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    require(digest == info["sha256"] and len(raw) == info["bytes"])
    artifact = json.loads(raw)
    scope = (
        subset_source_scope(artifact)
        if subset
        else strict_full_source_scope(artifact, SOURCE)
    )
    require(
        len(artifact["sources"]) == 2
        and len(artifact["listings"]) == len(scope["listings"])
    )
    validate_strict_artifact_freshness(artifact, require_strict_freshness=True)
    meta = artifact["runMeta"]
    start = _time(manifest["started_at"])
    fresh = meta["freshness"]
    require(
        fresh.get("generationId") == manifest["run_id"]
        and fresh.get("requireFreshDetails") is True
        and _time(fresh.get("generationStartedAt")) == start
    )
    collect_start, end = _time(meta["startedAt"]), _time(meta["finishedAt"])
    require(start <= collect_start <= end <= datetime.now(UTC) + timedelta(minutes=5))
    entries = {entry["transaction"]: entry for entry in artifact["sources"]}
    proofs = [entries[tx]["currentInventoryProof"] for tx in ("sale", "lease")]
    proof = proofs[0]
    for tx, item in zip(("sale", "lease"), proofs, strict=True):
        require(
            isinstance(item, dict)
            and item.get("schema") == schema
            and item.get("sourceKey") == SOURCE
            and item.get("transaction") == tx
            and item.get("kind")
            == (
                "verified_subset_with_holds" if subset else "complete_current_inventory"
            )
            and item.get("generationId") == manifest["run_id"]
            and _time(item.get("generationStartedAt")) == start
        )
        require(
            {k: v for k, v in item.items() if k != "transaction"}
            == {k: v for k, v in proof.items() if k != "transaction"}
        )
        if v2:
            require(item.get("subsetPolicy") == SUBSET_POLICY_V2)
    total = proof["total"]
    require(type(total) is int and total >= 100)
    observed = _time(proof["observedAt"])
    require(collect_start <= observed <= end)
    passes = proof["passes"]
    require(isinstance(passes, list) and len(passes) == 2)
    first, previous = _pass(passes[0], collect_start, observed, total)
    second, _ = _pass(passes[1], previous, observed, total)
    require(
        {url: row[0] for url, row in first.items()}
        == {url: row[0] for url, row in second.items()}
        and passes[1]["index"] == passes[0]["index"] + 1
    )
    cohort = proof["cohort"]
    require(isinstance(cohort, list) and len(cohort) == total)
    native, keys, urls = {}, set(), []
    for member in cohort:
        require(isinstance(member, list) and len(member) == 8)
        post, url, key, inv, inv_raw, detail, detail_raw, resolved = member
        _post(post)
        require(post not in native and key == _path_key(url) and key not in keys)
        require(url in second and (inv, inv_raw) == second[url])
        require(detail == _tenure(detail_raw) and resolved == _resolved(inv, detail))
        require(subset or resolved != "unknown")
        native[post] = (url, key, inv, inv_raw, detail, detail_raw, resolved)
        keys.add(key)
        urls.append(url)
    require(urls == sorted(second))
    details = {}
    require(isinstance(proof["details"], list) and len(proof["details"]) == total)
    for member in proof["details"]:
        require(isinstance(member, list) and len(member) == 4)
        post, canonical, clock, body_hash = member
        require(post in native and post not in details and native[post][0] == canonical)
        require(observed <= _time(clock) <= end)
        _digest(body_hash)
        details[post] = (canonical, clock, body_hash)
    empty_titles, title_urls, title_fallbacks = set(), [], {}
    if v2:
        observations = proof.get("titleObservations")
        require(isinstance(observations, list))
        for observation in observations:
            require(isinstance(observation, list) and len(observation) == 3)
            post, count, state = observation
            require(isinstance(post, str) and post in details and post not in empty_titles)
            require(type(count) is int and count == 1 and state == "empty")
            empty_titles.add(post)
            title_urls.append(native[post][0])
        require(title_urls == sorted(title_urls))
        fallback_urls = []
        fallbacks = proof.get("titleFallbackObservations", [])
        require(isinstance(fallbacks, list))
        for item in fallbacks:
            require(isinstance(item, dict) and set(item) == {"postId", "cardTitle", "documentTitle"})
            post, card, document = item["postId"], item["cardTitle"], item["documentTitle"]
            require(isinstance(post, str) and post in details and post not in title_fallbacks and post not in empty_titles)
            require(isinstance(card, str) and bool(card.strip()) and card == card.strip())
            require(document == f"{card} – Daum Commercial")
            title_fallbacks[post] = {"cardTitle": card, "documentTitle": document}
            fallback_urls.append(native[post][0])
        require(fallback_urls == sorted(fallback_urls))
    held = [[post, "empty_required_property_title" if post in empty_titles
             else "unknown_inventory_and_detail_tenure"]
            for post, row in native.items() if post in empty_titles or row[-1] == "unknown"]
    if subset:
        require(proof.get("held") == held)
        require(
            all(native[post][2] == native[post][4] == "unknown"
                for post, reason in held if reason == "unknown_inventory_and_detail_tenure")
        )
        _held_inventory_observations(proof, held, native)
        _held_detail_observations(proof, held, native, details, v2, empty_titles)
    retained = proof.get("retainedUnknownTenure", []) if subset else []
    require(isinstance(retained, list))
    require(not retained or retained == [post for post, reason in held
                                       if reason == "unknown_inventory_and_detail_tenure"])
    retained_ids = set(retained)
    if retained_ids:
        reason = "verified subset admitted additively with retained unknown-tenure records; whole source remains unqualified"
    held_ids = {post for post, _ in held} - retained_ids
    emitted_total = total - len(held_ids)
    require(emitted_total >= 100)
    listings = scope["listings"]
    staged, emitted = {}, {"sale": set(), "lease": set()}
    expected = {
        tx: {post for post, row in native.items()
             if post not in held_ids and (row[-1] in {tx, "sale_or_lease"} or (tx == "sale" and post in retained_ids))}
        for tx in ("sale", "lease")
    }
    for listing in listings:
        tx = listing["transactionMode"]
        require(tx in emitted)
        post = listing.get("daumPostId")
        require(post in expected[tx] and post not in emitted[tx])
        emitted[tx].add(post)
        url, key, inv, inv_raw, detail, detail_raw, resolved = native[post]
        canonical, clock, body_hash = details[post]
        evidence = listing.get("daumTenureEvidence")
        require(isinstance(evidence, dict))
        require(
            evidence.get("inventory") == inv
            and evidence.get("inventoryText") == inv_raw
            and evidence.get("detail") == detail
            and evidence.get("detailText") == detail_raw
            and evidence.get("resolved") == resolved
        )
        require(
            listing.get("id") == key
            and listing.get("url") == url
            and listing.get("canonicalUrl") == canonical
            and listing.get("daumObservedCanonicalUrl") == canonical
            and listing.get("transactionType")
            == {"sale": "Sale", "lease": "Lease", "sale_or_lease": "Sale/Lease", "unknown": None}[resolved]
            and listing.get("inventoryObservedAt") == proof["observedAt"]
            and listing.get("detailObservedAt") == clock
            and listing.get("daumDetailResponseSha256") == body_hash
        )
        if post in title_fallbacks:
            title = title_fallbacks[post]["cardTitle"]
            card = listing.get("daumSearchCard")
            require(isinstance(card, dict) and card.get("url") == url and card.get("title") == title)
            require(listing.get("daumTitleFallback") == title_fallbacks[post])
            require(listing.get("name") == title and listing.get("street") is None)
            require(all(listing.get(key) is None for key in ("city", "state", "postalCode")))
        else:
            require(listing.get("daumTitleFallback") is None)
        require(
            listing.get("preserveChildCollections", False) is (post in retained_ids or post in title_fallbacks)
            and (post in retained_ids or not listing.get("daumUnknownTenure"))
            and (post not in retained_ids or (listing.get("daumUnknownTenure") is True and listing.get("detailObservedWithChildPreservation") is True))
            and (post not in title_fallbacks or listing.get("detailObservedWithChildPreservation") is True)
            and not listing.get("detailError")
            and listing.get("inventoryOnly") is None
        )
        if post in retained_ids:
            inventory_observations = proof.get("heldInventoryObservations")
            detail_observations = proof.get("heldDetailObservations")
            require(isinstance(inventory_observations, list) and isinstance(detail_observations, list))
            inventory_by_id = {item["postId"]: item["inventory"] for item in inventory_observations}
            detail_by_id = {item["postId"]: item["detail"] for item in detail_observations}
            _validate_unknown_projection(
                listing, inventory_by_id[post], detail_by_id[post],
                generation=manifest["run_id"], inventory_at=proof["observedAt"],
                detail_at=clock, body_hash=body_hash,
            )
        provenance = listing.get("freshnessProvenance") or {}
        require(
            provenance.get("generationId") == manifest["run_id"]
            and provenance.get("detailScope") == "detail_page"
            and provenance.get("cacheDisposition") == "live"
            and provenance.get("method") == "daum_direct_detail_html"
            and provenance.get("identityMethod") == "wordpress_shortlink_post_id"
        )
        transformed = to_row(listing, {}, meta["finishedAt"])
        require(transformed is not None and transformed["external_id"] == key)
        if key in staged:
            staged[key] = merge_rows(staged[key], transformed)
        else:
            staged[key] = transformed
    require(emitted == expected and len(staged) == emitted_total)
    for tx in ("sale", "lease"):
        require(
            entries[tx]["listingsCollected"] == len(expected[tx])
            and entries[tx]["totalAvailableOnSource"] == len(expected[tx])
        )
    staged_identities = [
        [
            key,
            staged[key]["transaction_type"],
            next(post for post, row in native.items() if row[1] == key),
            staged[key]["source_url"],
        ]
        for key in sorted(staged)
    ]
    current, errors, sources, _, rejects = count_artifacts([str(artifact_path)], True)
    # Held members deliberately make this proved subset incomplete. The raw
    # gate must still see truncation; only this exact, independently validated
    # subset may proceed to its separate additive admission.
    expected_error = "source pass truncated" if subset and held else None
    require(
        sources == {SOURCE}
        and errors.get(SOURCE) == expected_error
        and rejects == 0
        and current.get(SOURCE) == len(listings)
    )
    validate_report_scope(report, manifest, SOURCE, artifact_path)
    summary, gate = report["summary"], report["per_source"][SOURCE]
    require(
        summary.get("mode") == "live"
        and type(summary.get("torow_errors")) is int
        and summary["torow_errors"] == 0
        and summary.get("baseline_updated") is False
        and summary.get("baseline_update_sources") == []
    )
    rechecking = (phase == "aggregate" or subset) and gate.get("verdict") == verdict
    expected_raw_reason = "no baseline row; cannot gate (first sight)"
    if expected_error:
        expected_raw_reason += "; a source pass also errored this run"
    require(
        (gate.get("raw_verdict") if rechecking else gate.get("verdict")) == "first_seen"
        and (gate.get("raw_reason") if rechecking else gate.get("reason"))
        == expected_raw_reason
        and gate.get("baseline_median") is None
        and gate.get("mark_missing_safe") is False
    )
    # The existing raw gate counts flat transaction-pass rows. Admission's
    # unchanged floor and staged count use the independently reconciled union.
    require(
        type(gate.get("current_active")) is int
        and gate["current_active"] == len(listings)
    )
    binding = {
        "schema": schema,
        "scope": scope_kind,
        "artifact_sha256": digest,
        "collector_git_sha": current_code_sha,
        "database_target_sha256": target["value"],
        "source_policy_sha256": configuration(SOURCE)["source_policy_sha256"],
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "observed_at": proof["observedAt"],
        "passes_sha256": _sha(passes),
        "cohort_sha256": _sha(cohort),
        "detail_receipts_sha256": _sha(proof["details"]),
        "emitted_count": emitted_total,
        "flat_emitted_count": len(listings),
        "staged_count": len(staged),
        "staged_identities_sha256": _sha(staged_identities),
    }
    if subset:
        binding.update(
            inventory_count=total,
            held_count=len(held_ids),
            held_sha256=_sha(held),
            sale_count=len(expected["sale"] - retained_ids),
            lease_count=len(expected["lease"]),
            whole_source_coverage=False,
            whole_lease_coverage="unknown" if held else "proved",
        )
        if "retainedUnknownTenure" in proof:
            binding["retained_unknown_tenure_count"] = len(retained_ids)
        if v2:
            binding.update(subset_policy=SUBSET_POLICY_V2,
                           title_observations_sha256=_sha(proof["titleObservations"]))
            if title_fallbacks:
                from cre_daum_fallback_partition import staged_fallback_members, transaction_guard_sql

                fallback_members = staged_fallback_members(artifact)
                probe = manifest["sources"][SOURCE].get("daum_fallback_probe")
                require(isinstance(probe, dict) and set(probe) == {
                    "artifact_sha256", "generation_id", "producer_sha",
                    "database_target_sha256", "members_sha256", "partition"})
                require(probe["artifact_sha256"] == digest
                        and probe["generation_id"] == manifest["run_id"]
                        and probe["producer_sha"] == current_code_sha
                        and probe["database_target_sha256"] == target["value"]
                        and probe["members_sha256"] == _sha(fallback_members))
                transaction_guard_sql(fallback_members, probe["partition"])
                binding["title_fallback_observations_sha256"] = _sha(proof["titleFallbackObservations"])
                binding["fallback_partition_sha256"] = _sha(probe["partition"])
                target_held = [
                    {"native_id": row["native_id"], "external_id": row["external_id"], "reason": row["hold_reason"]}
                    for row in probe["partition"] if row["hold_reason"] is not None
                ]
                target_held_ids = {row["native_id"] for row in target_held}
                target_held_keys = {row["external_id"] for row in target_held}
                binding.update(
                    source_emitted_count=emitted_total,
                    source_held_count=len(held_ids),
                    source_staged_identities_sha256=binding["staged_identities_sha256"],
                    target_held=target_held,
                    emitted_count=emitted_total - len(target_held_ids),
                    flat_emitted_count=sum(1 for row in listings if row["daumPostId"] not in target_held_ids),
                    staged_count=len(staged) - len(target_held_keys),
                    staged_identities_sha256=_sha([row for row in staged_identities if row[0] not in target_held_keys]),
                    held_count=len(held_ids) + len(target_held_ids),
                    sale_count=len((expected["sale"] - retained_ids) - target_held_ids),
                    lease_count=len(expected["lease"] - target_held_ids),
                    whole_lease_coverage="unknown" if held or target_held_ids else "proved",
                )
                require(binding["emitted_count"] > 0)
    if phase == "aggregate":
        require(
            manifest["sources"][SOURCE].get("gate", {}).get("current_inventory_proof")
            == binding
        )
    if rechecking:
        require(
            gate.get("current_inventory_proof") == binding
            and gate.get("admission_scope") == scope_kind
            and gate.get("reason") == reason
        )
    gate.update(
        raw_verdict="first_seen",
        raw_reason=expected_raw_reason,
        verdict=verdict,
        reason=reason,
        mark_missing_safe=False,
        admission_scope=scope_kind,
        current_inventory_proof=binding,
    )
    report["scope"] = {
        "kind": "collector_registry_verified_subset" if subset else SCOPE,
        "transactions": ["sale", "lease"],
        "whole_source_coverage": False,
    }
    summary.update(mark_missing_safe_sources=[], mark_missing_safe_brokerages=[])
    manifest["scope"].update(
        kind="collector_registry_verified_subset"
        if subset
        else "collector_registry_additive_current_inventory",
        whole_source_coverage=False,
    )
    return binding


def ingest_subset_context(
    artifact_path,
    envelope,
    *,
    current_code_sha,
    current_git_dirty,
    expected_database_target_sha256,
    attempt_id,
):
    """Reprove both sealed gate decisions at the final ingest boundary."""
    require(
        isinstance(envelope, dict)
        and set(envelope)
        == {"manifest", "source_report", "aggregate_report", "binding", "attempt_id"}
    )
    require(
        isinstance(attempt_id, str)
        and re.fullmatch(r"[a-f0-9]{32}", attempt_id)
        and envelope["attempt_id"] == attempt_id
    )
    manifest = copy.deepcopy(envelope["manifest"])
    require(
        manifest["preflight"]["database_target"]
        == {"algorithm": "sha256", "value": expected_database_target_sha256}
    )
    binding = envelope["binding"]
    require(isinstance(binding, dict) and binding.get("scope") in {SUBSET_SCOPE, SUBSET_SCOPE_V2})
    for phase, key in (("source", "source_report"), ("aggregate", "aggregate_report")):
        report = copy.deepcopy(envelope[key])
        require(report["per_source"][SOURCE].get("current_inventory_proof") == binding)
        require(
            admit_subset(
                Path(artifact_path),
                manifest,
                report,
                current_code_sha=current_code_sha,
                current_git_dirty=current_git_dirty,
                phase=phase,
            )
            == binding
        )
    raw = Path(artifact_path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == binding["artifact_sha256"])
    proof = json.loads(raw)["sources"][0]["currentInventoryProof"]
    held = {member[0] for member in proof["held"]} - set(proof.get("retainedUnknownTenure", []))
    result = {
        "schema": proof["schema"],
        "binding": binding,
        "attempt_id": attempt_id,
        "members": [
            {
                "native_id": row[0],
                "source_url": row[1],
                "external_id": row[2],
                "emitted": row[0] not in held,
            }
            for row in proof["cohort"]
        ],
    }
    if proof["schema"] == SUBSET_SCHEMA_V2:
        result.update(subset_policy=SUBSET_POLICY_V2, titleObservations=proof["titleObservations"])
        if proof.get("titleFallbackObservations"):
            from cre_daum_fallback_partition import staged_fallback_members

            result["titleFallbackObservations"] = proof["titleFallbackObservations"]
            result["fallbackMembers"] = staged_fallback_members(json.loads(raw))
            result["fallbackPartition"] = manifest["sources"][SOURCE]["daum_fallback_probe"]["partition"]
            require(_sha(result["fallbackPartition"]) == binding["fallback_partition_sha256"])
    return result
