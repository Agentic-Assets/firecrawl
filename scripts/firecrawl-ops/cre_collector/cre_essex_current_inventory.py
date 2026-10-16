"""Pure Essex absent-baseline admission; closed archive is coverage, never output."""

from __future__ import annotations

import hashlib
import json
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
    validate_report_scope,
)
from cre_gate import count_artifacts
from cre_ingest import (
    strict_full_source_scope,
    to_row,
    validate_strict_artifact_freshness,
)
from cre_source_policy import load_source_policy

SOURCE = "essex-realty"
SCHEMA = "essex-strict-detail-v2"
BASE = "https://essexrealtygroup.com/properties/"
ENDPOINTS = {
    "current": BASE,
    "closed": BASE + "?sale_deal_status_id=3",
    "sitemap": "https://essexrealtygroup.com/properties-sitemap.xml",
}
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
        isinstance(value, str) and not any(c.isspace() or ord(c) < 32 for c in value)
    )
    url = urlsplit(value)
    require(
        url.scheme == "https"
        and url.netloc == "essexrealtygroup.com"
        and not url.query
        and not url.fragment
        and re.fullmatch(r"/properties/[^/]+/", url.path)
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


def _pass(snapshot, previous, observed):
    require(isinstance(snapshot, dict))
    parsed = {}
    for role in ("current", "closed", "sitemap"):
        page = snapshot[role]
        require(isinstance(page, dict) and page.get("endpoint") == ENDPOINTS[role])
        _digest(page.get("responseTextSha256"))
        clock = _time(page.get("observedAt"))
        require(previous <= clock <= observed)
        previous = clock
        if role == "sitemap":
            urls = page["urls"]
            require(isinstance(urls, list) and bool(urls))
            for url in urls:
                _path_key(url)
            require(len(urls) == len(set(urls)))
            parsed[role] = sorted(urls)
            continue
        rows = page["rows"]
        require(
            isinstance(rows, list)
            and bool(rows)
            and type(page.get("declaredTotal")) is int
            and page["declaredTotal"] == len(rows)
        )
        urls = []
        for row in rows:
            require(isinstance(row, list) and len(row) == 5)
            url, lifecycle, name, location, units = row
            _path_key(url)
            require(
                lifecycle
                in (
                    {"on_market", "under_contract"} if role == "current" else {"closed"}
                )
            )
            require(isinstance(name, str) and bool(name.strip()))
            require(location is None or isinstance(location, str))
            require(units is None or (type(units) is int and units > 0))
            urls.append(url)
        require(len(urls) == len(set(urls)))
        parsed[role] = sorted(rows, key=lambda row: row[0])
    current = {row[0] for row in parsed["current"]}
    closed = {row[0] for row in parsed["closed"]}
    require(not current & closed and current | closed == set(parsed["sitemap"]))
    fingerprint = _sha([parsed["current"], parsed["closed"], parsed["sitemap"]])
    require(snapshot.get("fingerprint") == fingerprint)
    return parsed, previous


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
    for tx, item in zip(("sale", "lease"), proofs, strict=True):
        require(
            isinstance(item, dict)
            and item.get("schema") == SCHEMA
            and item.get("sourceKey") == SOURCE
            and item.get("transaction") == tx
            and item.get("generationId") == manifest["run_id"]
            and _time(item.get("generationStartedAt")) == start
        )
        excluded = {"transaction", "kind", "reason", "details"}
        require(
            {k: v for k, v in item.items() if k not in excluded}
            == {k: v for k, v in proof.items() if k not in excluded}
        )
    require(
        proof.get("kind") == "complete_current_inventory"
        and proof.get("reason") is None
    )
    lease = proofs[1]
    require(
        lease.get("kind") == "known_empty_transaction"
        and lease.get("reason") == "sale_only_source"
        and lease.get("details") == []
        and entries["lease"]["listingsCollected"] == 0
        and entries["lease"]["totalAvailableOnSource"] == 0
    )
    total = proof["total"]
    require(
        type(total) is int and total > 0
    )  # absent-baseline proof has no historical floor
    observed = _time(proof["observedAt"])
    require(collect_start <= observed <= end)
    passes = proof["passes"]
    require(isinstance(passes, list) and len(passes) == 2)
    first, previous = _pass(passes[0], collect_start, observed)
    second, _ = _pass(passes[1], previous, observed)
    require(first == second and len(second["current"]) == total)
    cohort = proof["cohort"]
    require(isinstance(cohort, list) and len(cohort) == total)
    native, keys = {}, set()
    for member, archive in zip(cohort, second["current"], strict=True):
        require(isinstance(member, list) and len(member) == 5)
        post, url, key, lifecycle, raw_property_id = member
        _post(raw_property_id)
        _post(post)
        require(
            post not in native
            and key == _path_key(url)
            and key not in keys
            and [url, lifecycle] == archive[:2]
        )
        native[post] = (url, key, lifecycle, raw_property_id)
        keys.add(key)
    details = {}
    require(isinstance(proof["details"], list) and len(proof["details"]) == total)
    for member in proof["details"]:
        require(isinstance(member, list) and len(member) == 4)
        post, canonical, clock, body_hash = member
        require(
            post in native
            and post not in details
            and native[post][0] == canonical
            and observed <= _time(clock) <= end
        )
        _digest(body_hash)
        details[post] = (canonical, clock, body_hash)
    listings = scope["listings"]
    require(
        len(listings) == total
        and entries["sale"]["listingsCollected"] == total
        and entries["sale"]["totalAvailableOnSource"] == total
    )
    staged, emitted = {}, set()
    for listing in listings:
        page = listing["essexWordpressPage"]
        require(isinstance(page, dict) and set(page) == {"type", "id", "canonicalUrl"})
        require(page["type"] == "wordpress_properties_post")
        post = page["id"]
        require(post in native and post not in emitted)
        emitted.add(post)
        url, key, lifecycle, raw_property_id = native[post]
        canonical, clock, body_hash = details[post]
        require(
            listing.get("essexPropertyId") == raw_property_id
            and page["canonicalUrl"] == canonical
            and listing.get("id") == key
            and listing.get("url") == url
            and listing.get("canonicalUrl") == canonical
            and listing.get("lifecycle") == lifecycle
            and listing.get("transactionMode") == "sale"
            and listing.get("transactionType") == "Sale"
        )
        require(
            listing.get("inventoryObservedAt") == proof["observedAt"]
            and listing.get("detailObservedAt") == clock
            and listing.get("essexDetailResponseSha256") == body_hash
        )
        require(
            not listing.get("preserveChildCollections")
            and not listing.get("detailError")
            and listing.get("inventoryOnly") is None
        )
        provenance = listing.get("freshnessProvenance") or {}
        require(
            provenance.get("generationId") == manifest["run_id"]
            and provenance.get("observedAt") == clock
            and provenance.get("detailScope") == "detail_page"
            and provenance.get("cacheDisposition") == "live"
            and provenance.get("method") == "essex_direct_detail_html"
            and provenance.get("identityMethod")
            == "literal_owned_property_path+wordpress_properties_post"
        )
        transformed = to_row(listing, {}, meta["finishedAt"])
        require(
            transformed is not None
            and transformed["external_id"] == key
            and key not in staged
        )
        staged[key] = [key, transformed["transaction_type"], post, url]
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
    require(type(gate.get("current_active")) is int and gate["current_active"] == total)
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
        "passes_sha256": _sha(passes),
        "cohort_sha256": _sha(cohort),
        "detail_receipts_sha256": _sha(proof["details"]),
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
