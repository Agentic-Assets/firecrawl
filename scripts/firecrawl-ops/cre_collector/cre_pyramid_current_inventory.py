"""Pure Pyramid coverage admission with strict fresh detail and literal path IDs."""

from __future__ import annotations

import hashlib
import json
import re
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
from cre_gate import DEFAULT_FLOOR, count_artifacts
from cre_ingest import (
    strict_full_source_scope,
    to_row,
    validate_strict_artifact_freshness,
)
from cre_source_policy import load_source_policy

SOURCE = "pyramid-brokerage"
SCHEMA = "pyramid-strict-detail-v1"
HOST = "www.pyramidbrokerage.com"
ENDPOINT = f"https://{HOST}/wp-json/wp/v2/pbc-listings"
REASON = (
    "complete current inventory admitted additively; historical baseline remains absent"
)


def _sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value))


def _path_key(value):
    require(
        isinstance(value, str) and not any(c.isspace() or ord(c) < 32 for c in value)
    )
    url = urlsplit(value)
    require(
        url.scheme == "https"
        and url.netloc == HOST
        and not url.query
        and not url.fragment
    )
    require(
        url.path.startswith("/listings/") and url.path[len("/listings/") :].strip("/")
    )
    return url.path.strip("/")


def admit(
    artifact_path: Path,
    manifest: dict,
    report: dict,
    *,
    current_code_sha: str,
    current_git_dirty: bool,
    phase: str,
    source=SOURCE,
) -> dict:
    require(source == SOURCE and phase in {"source", "aggregate"})
    config = manifest["config"]
    # Initial extension is deliberately singleton, even inside a pipeline owner.
    require(
        config["sources"] == [SOURCE] and config["transactions"] == ["sale", "lease"]
    )
    require(admission_configuration(config, SOURCE) == configuration(SOURCE))
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
    scope = strict_full_source_scope(artifact, SOURCE)
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
    for tx, item in zip(("sale", "lease"), proofs):
        require(isinstance(item, dict))
        require(
            item.get("schema") == SCHEMA
            and item.get("sourceKey") == SOURCE
            and item.get("transaction") == tx
            and item.get("generationId") == manifest["run_id"]
            and _time(item.get("generationStartedAt")) == start
        )
        require(
            {k: v for k, v in item.items() if k not in {"transaction", "details"}}
            == {k: v for k, v in proof.items() if k not in {"transaction", "details"}}
        )
    total, total_pages, cohort = proof["total"], proof["totalPages"], proof["cohort"]
    require(
        type(total) is int
        and total >= DEFAULT_FLOOR
        and type(total_pages) is int
        and total_pages == (total + 99) // 100
        and isinstance(cohort, list)
        and len(cohort) == total
    )
    native, keys = {}, set()
    for member in cohort:
        require(isinstance(member, list) and len(member) == 4)
        post, url, key, tx = member
        require(
            type(post) is int and 0 < post <= 9007199254740991 and post not in native
        )
        require(key == _path_key(url) and key not in keys and tx in {"sale", "lease"})
        native[post] = (url, key, tx)
        keys.add(key)
    require(list(native) == sorted(native))
    observed = _time(proof["observedAt"])
    require(collect_start <= observed <= end)
    passes = proof["passes"]
    require(isinstance(passes, list) and len(passes) == 2)
    require(passes[0]["fingerprint"] == passes[1]["fingerprint"])
    previous_end = collect_start
    for snapshot in passes:
        require(isinstance(snapshot, dict))
        _digest(snapshot["fingerprint"])
        pages = snapshot["pages"]
        require(isinstance(pages, list) and len(pages) == total_pages)
        times = []
        for index, page in enumerate(pages, 1):
            require(isinstance(page, list) and len(page) == 7)
            number, endpoint, clock, body_hash, page_total, page_count, ids = page
            require(
                all(type(value) is int for value in (number, page_total, page_count))
                and number == index
                and page_total == total
                and page_count == total_pages
            )
            url = urlsplit(endpoint)
            require(
                url.scheme == "https"
                and url.netloc == HOST
                and url.path == urlsplit(ENDPOINT).path
                and not url.fragment
                and parse_qs(url.query, strict_parsing=True)
                == {
                    "per_page": ["100"],
                    "orderby": ["id"],
                    "order": ["asc"],
                    "page": [str(index)],
                    "_embed": ["wp:term"],
                }
            )
            require(isinstance(ids, list) and all(type(value) is int for value in ids))
            require(ids == list(native)[(index - 1) * 100 : index * 100])
            _digest(body_hash)
            clock = _time(clock)
            require(previous_end <= clock <= observed)
            times.append(clock)
        previous_end = max(times)
    details = {}
    for tx, item in zip(("sale", "lease"), proofs):
        members = item["details"]
        require(isinstance(members, list))
        for member in members:
            require(isinstance(member, list) and len(member) == 4)
            post, canonical, clock, body_hash = member
            require(type(post) is int and post in native and post not in details)
            require(native[post][0] == canonical and native[post][2] == tx)
            require(observed <= _time(clock) <= end)
            _digest(body_hash)
            details[post] = (canonical, clock, body_hash)
        count = sum(member[2] == tx for member in native.values())
        require(
            len(members) == count
            and entries[tx]["listingsCollected"] == count
            and entries[tx]["totalAvailableOnSource"] == count
        )
    require(set(details) == set(native))
    listings = scope["listings"]
    require(len(listings) == total)
    staged, emitted = {}, set()
    for listing in listings:
        post = listing["wordpressRecord"]["id"]
        require(type(post) is int and post in native and post not in emitted)
        emitted.add(post)
        url, key, tx = native[post]
        canonical, clock, body_hash = details[post]
        require(
            listing.get("id") == key
            and listing.get("url") == url
            and listing.get("canonicalUrl") == canonical
            and listing.get("transactionMode") == tx
            and listing.get("transactionType") == tx.title()
        )
        require(
            listing.get("inventoryObservedAt") == proof["observedAt"]
            and listing.get("detailObservedAt") == clock
            and listing["wordpressRecord"].get("detailResponseSha256") == body_hash
        )
        require(
            all(
                isinstance(listing.get(k), str) and listing[k].strip()
                for k in ("state", "city", "assetType")
            )
        )
        require(
            not listing.get("preserveChildCollections")
            and not listing.get("detailError")
            and listing.get("inventoryOnly") is None
        )
        provenance = listing.get("freshnessProvenance") or {}
        require(
            provenance.get("generationId") == manifest["run_id"]
            and provenance.get("detailScope") == "detail_page"
            and provenance.get("cacheDisposition") == "live"
            and provenance.get("method") == "pyramid_direct_detail_html"
            and provenance.get("identityMethod")
            == "literal_owned_listing_path+wordpress_post_id"
        )
        transformed = to_row(listing, {}, meta["finishedAt"])
        require(
            transformed is not None
            and transformed["external_id"] == key
            and key not in staged
        )
        staged[key] = [key, transformed["transaction_type"], post, url]
    require(len(staged) == total and len(staged) >= DEFAULT_FLOOR)
    current, errors, sources, _, rejects = count_artifacts([str(artifact_path)], True)
    require(
        sources == {SOURCE}
        and not any(errors.values())
        and rejects == 0
        and current.get(SOURCE) == total
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
    rechecking = phase == "aggregate" and gate.get("verdict") == VERDICT
    require(
        (gate.get("raw_verdict") if rechecking else gate.get("verdict")) == "first_seen"
        and (gate.get("raw_reason") if rechecking else gate.get("reason"))
        == "no baseline row; cannot gate (first sight)"
        and gate.get("baseline_median") is None
        and gate.get("mark_missing_safe") is False
    )
    require(
        type(gate.get("current_active")) is int
        and gate["current_active"] == total
        and type(gate.get("effective_floor")) is int
        and gate["effective_floor"] == DEFAULT_FLOOR
    )
    binding = {
        "schema": SCHEMA,
        "scope": SCOPE,
        "artifact_sha256": digest,
        "collector_git_sha": current_code_sha,
        "database_target_sha256": target["value"],
        "source_policy_sha256": configuration(SOURCE)["source_policy_sha256"],
        "generation_id": manifest["run_id"],
        "generation_started_at": manifest["started_at"],
        "observed_at": proof["observedAt"],
        "pages_sha256": _sha(passes),
        "cohort_sha256": _sha(cohort),
        "detail_receipts_sha256": _sha([item["details"] for item in proofs]),
        "emitted_count": total,
        "staged_count": len(staged),
        "staged_identities_sha256": _sha([staged[k] for k in sorted(staged)]),
    }
    if phase == "aggregate":
        require(
            manifest["sources"][SOURCE].get("gate", {}).get("current_inventory_proof")
            == binding
        )
    if rechecking:
        require(
            gate.get("current_inventory_proof") == binding
            and gate.get("admission_scope") == SCOPE
            and gate.get("reason") == REASON
        )
    gate.update(
        raw_verdict="first_seen",
        raw_reason="no baseline row; cannot gate (first sight)",
        verdict=VERDICT,
        reason=REASON,
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
