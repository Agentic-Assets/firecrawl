"""Pure DAUM complete strict-detail absent-baseline additive admission."""

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
    merge_rows,
    validate_strict_artifact_freshness,
)
from cre_source_policy import load_source_policy

SOURCE = "daum-commercial"
SCHEMA = "daum-strict-detail-v1"
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
            and item.get("kind") == "complete_current_inventory"
            and item.get("generationId") == manifest["run_id"]
            and _time(item.get("generationStartedAt")) == start
        )
        require(
            {k: v for k, v in item.items() if k != "transaction"}
            == {k: v for k, v in proof.items() if k != "transaction"}
        )
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
        require(resolved != "unknown")
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
    listings = scope["listings"]
    staged, emitted = {}, {"sale": set(), "lease": set()}
    expected = {
        tx: {post for post, row in native.items() if row[-1] in {tx, "sale_or_lease"}}
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
            == {"sale": "Sale", "lease": "Lease", "sale_or_lease": "Sale/Lease"}[
                resolved
            ]
            and listing.get("inventoryObservedAt") == proof["observedAt"]
            and listing.get("detailObservedAt") == clock
            and listing.get("daumDetailResponseSha256") == body_hash
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
            and provenance.get("method") == "daum_direct_detail_html"
            and provenance.get("identityMethod") == "wordpress_shortlink_post_id"
        )
        transformed = to_row(listing, {}, meta["finishedAt"])
        require(transformed is not None and transformed["external_id"] == key)
        if key in staged:
            staged[key] = merge_rows(staged[key], transformed)
        else:
            staged[key] = transformed
    require(emitted == expected and len(staged) == total)
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
    require(
        sources == {SOURCE}
        and not any(errors.values())
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
    rechecking = phase == "aggregate" and gate.get("verdict") == VERDICT
    require(
        (gate.get("raw_verdict") if rechecking else gate.get("verdict")) == "first_seen"
        and (gate.get("raw_reason") if rechecking else gate.get("reason"))
        == "no baseline row; cannot gate (first sight)"
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
        "flat_emitted_count": len(listings),
        "staged_count": len(staged),
        "staged_identities_sha256": _sha(staged_identities),
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
