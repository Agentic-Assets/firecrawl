"""One-source Buildout feed admission; no network, SQL or baseline writes."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cre_current_inventory_admission import (
    SCOPE,
    VERDICT,
    _time,
    admission_configuration,
    configuration,
    require,
    validate_report_scope,
)
from cre_gate import count_artifacts
from cre_ingest import (
    BUILDOUT_SOURCE_KEYS,
    merge_rows,
    strict_full_source_scope,
    to_row,
)

SCHEMA = "buildout-current-inventory-v1"
SORT = "created_at asc, id asc"


def registry_path() -> Path:
    return Path(__file__).resolve().parent / "data" / "cre-buildout-sources.json"


def definitions() -> dict:
    """Load the same public source coordinates consumed by the TS registry."""
    rows = json.loads(registry_path().read_text())
    require(isinstance(rows, list) and len(rows) == 25)
    result = {}
    for row in rows:
        require(isinstance(row, list) and len(row) == 5)
        source, company, plugin, page = row[:4]
        require(all(isinstance(v, str) and v for v in row))
        require(source not in result and re.fullmatch(r"[a-f0-9]{40}", plugin))
        url = urlsplit(page)
        require(url.scheme == "https" and url.hostname and url.netloc == url.hostname)
        require(not url.fragment)
        require(row[4] == url.hostname)
        result[source] = {
            "company": company,
            "plugin": plugin,
            "page": page,
            "host": url.hostname,
        }
    require(
        set(result)
        == set(BUILDOUT_SOURCE_KEYS) - {"svn", "lee-associates", "franklin-street"}
    )
    require(len({row["plugin"] for row in result.values()}) == len(result))
    return result


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _digest(value: object) -> None:
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))


def _listing_hosts_match(actual: str, expected: str) -> bool:
    """Compare the same DNS host with exactly one optional leading www. label."""
    return actual.lower().removeprefix("www.") == expected.lower().removeprefix("www.")


def _owned_url(value: object, host: str) -> str:
    require(
        isinstance(value, str)
        and value
        and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
    )
    parsed = urlsplit(value)
    require(
        parsed.scheme in {"http", "https"}
        and parsed.hostname
        and parsed.netloc.lower() == parsed.hostname
        and _listing_hosts_match(parsed.hostname, host)
        and "#" not in value
    )
    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if "propertyId" in query:
        # Some official feeds retain an empty template parameter before their
        # actual propertyId. Preserve it; ingest selects the first nonempty
        # literal value. Multiple nonempty values remain ambiguous.
        ids = [item for item in query["propertyId"] if item]
        require(len(ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", ids[0]))
        require(bool(re.sub(r"-(sale|lease)$", "", ids[0])))
        literal = re.search(r"[?&]propertyId=([^&#]+)", value)
        require(literal is not None and literal.group(1) == ids[0])
    else:
        # Path-routed official listings retain to_row's native-ID fallback.
        # Never turn a homepage, inventory hub or query-only URL into a listing.
        require(not parsed.query)
        require(re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+[A-Za-z0-9_-]+/?", parsed.path))
        require(
            parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
            not in {
                "properties",
                "listings",
                "inventory",
                "search",
                "property-search",
                "available-properties",
            }
        )
    return value


def _url_identity(value: str) -> tuple:
    """Compare admitted URLs without changing literal URLs or ingest IDs."""
    parsed = urlsplit(value)
    literal = re.search(r"[?&]propertyId=([^&#]+)", value)
    return (
        parsed.scheme,
        (parsed.hostname or "").removeprefix("www."),
        parsed.path,
        re.sub(r"-(sale|lease)$", "", literal.group(1)) if literal else None,
        # Keep unrelated routing parameters literal and ordered: repeated query
        # keys can select different resources depending on which value is first.
        tuple(
            part
            for part in parsed.query.split("&")
            if not part.startswith("propertyId=")
        ),
    )


def admit(
    artifact_path: Path,
    manifest: dict,
    report: dict,
    *,
    current_code_sha: str,
    current_git_dirty: bool,
    phase: str,
    source: str | None = None,
) -> dict:
    config = manifest["config"]
    if source is None:
        source = config.get("current_inventory_admission", {}).get("source_key")
    admission = admission_configuration(config, source)
    registry = definitions()
    require(source in registry)
    definition = registry[source]
    require(admission == configuration(source))
    require(
        config.get("additive") is True
        and config.get("mark_missing") is False
        and config.get("status_activation") is False
    )
    require(config.get("max_items") == 0)
    require(manifest.get("collector_git_dirty") is False and current_git_dirty is False)
    require(
        re.fullmatch(r"[0-9a-f]{40}", current_code_sha)
        and manifest["collector_git_sha"] == current_code_sha
    )
    target = manifest["preflight"]["database_target"]
    require(isinstance(target, dict) and target.get("algorithm") == "sha256")
    _digest(target.get("value"))
    require(phase in {"source", "aggregate"})
    info = manifest["sources"][source]["artifact"]
    require(not artifact_path.is_symlink() and artifact_path.is_file())
    raw = artifact_path.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest()
    require(actual_hash == info["sha256"] and len(raw) == info["bytes"])
    artifact = json.loads(raw)
    scope = strict_full_source_scope(artifact, source)
    meta = artifact["runMeta"]
    fresh = meta.get("freshness") or {}
    start = _time(manifest["started_at"])
    now = datetime.now(UTC)
    require(
        fresh.get("generationId") == manifest["run_id"]
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
            proof.get("sourceKey") == source
            and proof.get("transaction") == tx
            and proof.get("generationId") == manifest["run_id"]
        )
        require(
            proof.get("pluginKey") == definition["plugin"]
            and proof.get("listingsPage") == definition["page"]
            and proof.get("sort") == SORT
        )
    proof = by_tx["sale"]["currentInventoryProof"]
    other = by_tx["lease"]["currentInventoryProof"]
    require(
        {k: v for k, v in proof.items() if k != "transaction"}
        == {k: v for k, v in other.items() if k != "transaction"}
    )
    total, limit = proof.get("total"), proof.get("limit")
    require(type(total) is int and total > 0 and type(limit) is int and limit > 0)
    pages = proof.get("pages")
    require(
        isinstance(pages, list)
        and len(pages) == (total + limit - 1) // limit
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
            page.get("generationId") == manifest["run_id"]
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
    }
    require(expected)
    listings = scope["listings"]
    require(
        len(listings) == len(expected)
        and len({r.get("id") for r in listings}) == len(expected)
    )
    require({r.get("id") for r in listings} == set(expected))
    staged = defaultdict(list)
    stored_id_by_url = {}
    for listing in listings:
        item, page = expected[listing["id"]]
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
        require(
            provenance.get("generationId") == manifest["run_id"]
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
            and entry["totalAvailableOnSource"] == total
        )
    current, errors, sources, _, rejected = count_artifacts([str(artifact_path)], True)
    require(
        sources == {source}
        and not any(errors.values())
        and rejected == 0
        and current.get(source) == len(listings)
    )
    validate_report_scope(report, manifest, source, artifact_path)
    summary, gate = report["summary"], report["per_source"][source]
    require(
        summary.get("mode") == "live"
        and type(summary.get("torow_errors")) is int
        and summary["torow_errors"] == 0
        and summary.get("baseline_updated") is False
        and summary.get("baseline_update_sources") == []
    )
    require(
        gate.get("verdict") == "first_seen"
        and gate.get("reason") == "no baseline row; cannot gate (first sight)"
        and gate.get("baseline_median") is None
        and gate.get("mark_missing_safe") is False
    )
    require(
        type(gate.get("current_active")) is int
        and gate["current_active"] == len(listings)
    )
    binding = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "artifact_sha256": actual_hash,
        "collector_git_sha": current_code_sha,
        "database_target_sha256": target["value"],
        "source_policy_sha256": admission["source_policy_sha256"],
        "buildout_registry_sha256": admission["buildout_registry_sha256"],
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "observed_at": min(times),
        "pages_sha256": _sha(pages),
        "raw_feed_count": total,
        "emitted_count": len(listings),
        "staged_count": len(staged),
        "staged_identities_sha256": _sha(staged_bindings),
    }
    if phase == "aggregate":
        require(
            manifest["sources"][source].get("gate", {}).get("current_inventory_proof")
            == binding
        )
    gate.update(
        raw_verdict=gate["verdict"],
        raw_reason=gate["reason"],
        verdict=VERDICT,
        reason="complete current inventory admitted additively; historical baseline remains absent",
        mark_missing_safe=False,
        admission_scope=SCOPE,
        current_inventory_proof=binding,
    )
    report["scope"] = {
        "kind": SCOPE,
        "transactions": ["sale", "lease"],
        "whole_source_coverage": False,
    }
    summary.update(mark_missing_safe_sources=[], mark_missing_safe_brokerages=[])
    manifest["scope"].update(
        kind="collector_registry_additive_current_inventory",
        whole_source_coverage=False,
    )
    return binding
