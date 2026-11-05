"""Offline planner for mixed Franklin Buildout success and site-page fallback."""
import json
from datetime import UTC, datetime

from cre_buildout_current_inventory import SCHEMA, SORT, _url_identity
from cre_current_inventory_admission import _time, require
from cre_franklin_site_projection import project
from cre_franklin_site_sql import build_sql as site_sql
from cre_ingest import (
    apply_status_activation_gate,
    artifact_run_identity_from_digests,
    build_sql,
    to_row,
    validate_strict_artifact_freshness,
)
from source_fields.buildout import _core_definitions, _validate_feed, qualified_counts


def plan(raw, binding, held, env, *, now=None):
    """Plan both independently recoverable transactions without making writes.

    The foreground owner must bind target, code, immutable attempt and worker
    settlement, then run/read back each lane under its existing recovery fence.
    """
    projection = project(raw, binding["generation_id"], env)
    if projection["artifact_sha256"] != binding["artifact_sha256"]:
        raise ValueError("Franklin artifact digest differs from binding")
    artifact = json.loads(raw)
    meta = artifact.get("runMeta") or {}
    freshness = meta.get("freshness") or {}
    if (meta.get("mode") != "full" or meta.get("transactions") != ["sale", "lease"]
            or meta.get("maxItemsPerSource") is not None
            or meta.get("startedAt") != binding["started_at"] or meta.get("finishedAt") != binding["finished_at"]
            or freshness.get("generationId") != binding["generation_id"]
            or freshness.get("generationStartedAt") != binding["generation_started_at"]
            or freshness.get("requireFreshDetails") is not True
            or meta.get("terminalCaptureFailure")):
        raise ValueError("Franklin mixed source scope differs")
    rows = projection["projection"]["buildout"]["listings"]
    entries = artifact["sources"]
    if len(entries) != 2 or {entry.get("transaction") for entry in entries} != {"sale", "lease"}:
        raise ValueError("Franklin mixed source requires exact transaction scope")
    saved_rows = []
    if rows:
        validate_strict_artifact_freshness(artifact, require_strict_freshness=True, now=now)
        now = now or datetime.now(UTC)
        start, finish = _time(binding["generation_started_at"]), _time(meta["finishedAt"])
        require(start <= _time(meta["startedAt"]) <= finish)
        definitions = _core_definitions()["franklin-street"]
        brokers = {i: value for i, value in enumerate(artifact["brokers"])}
        candidates = qualified_counts(artifact, binding["artifact_sha256"])
        seen, seen_urls = set(), set()
        for entry in entries:
            tx = entry["transaction"]
            if entry.get("error"):
                continue
            definition = definitions[tx]
            proof = entry.get("currentInventoryProof") or {}
            require(
                entry.get("sourceKey") == "franklin-street" and entry.get("supported") is True
                and entry.get("truncated") is False and proof.get("schema") == SCHEMA
                and proof.get("kind") == "complete_paginated_feed"
                and proof.get("sourceKey") == f"franklin-street-{tx}"
                and proof.get("transaction") == tx and proof.get("generationId") == binding["generation_id"]
                and proof.get("pluginKey") == definition["plugin"] and proof.get("listingsPage") == definition["page"]
                and proof.get("sort") == SORT
            )
            selected, native, _, _, total = _validate_feed(proof, definition, binding["generation_id"],
                                                          start, finish, now, rows, tx)
            require(entry.get("listingsCollected") == len(selected) and entry.get("totalAvailableOnSource") == total)
            for row in selected:
                item, page = native[row["id"]]
                expected_tx = "Sale/Lease" if item["alsoForSaleOrLease"] else ("Sale" if item["sale"] else "Lease")
                provenance = row.get("freshnessProvenance") or {}
                require(row.get("transactionType") == expected_tx and row.get("url") == item["showLink"]
                        and row.get("canonicalUrl") == item["showLink"]
                        and row.get("underContract", False) is item["underContract"]
                        and row.get("statusBadge") == ("under_contract" if item["underContract"] else None)
                        and row.get("preserveChildCollections") is True
                        and row.get("inventoryObservedAt") == page["observedAt"]
                        and provenance.get("generationId") == binding["generation_id"]
                        and provenance.get("cacheDisposition") == page["cacheDisposition"]
                        and provenance.get("method") == "buildout_inventory_feed"
                        and provenance.get("detailScope") == "authoritative_inventory_feed"
                        and not row.get("detailObservedAt"))
                mapped = to_row(row, brokers, meta["finishedAt"], candidates)
                require(mapped is not None)
                identity = mapped["external_id"]
                url_identity = _url_identity(row["url"])
                require(identity not in seen and url_identity not in seen_urls)
                seen.add(identity)
                seen_urls.add(url_identity)
                mapped.pop("_modes", None)
                saved_rows.append(mapped)
        require(len(saved_rows) == len(rows))
    apply_status_activation_gate(saved_rows, False)
    job_key, _ = artifact_run_identity_from_digests([binding["artifact_sha256"]])
    buildout = None
    if saved_rows:
        job = [{"slug": "franklin-street", "discovered": len(rows), "saved": len(saved_rows),
                "errors": 1, "notes": "Franklin mixed partial; Buildout peers only"}]
        buildout = build_sql(saved_rows, job, meta["startedAt"], set(),
                             artifact_run_key=job_key, finished_at=meta["finishedAt"])
    site_rows = projection["projection"]["listings"]
    if site_rows:
        site = site_sql(site_rows, held, binding)
    else:
        if held:
            raise ValueError("held identities without Franklin site rows")
        site = None
    return {"coverage": "partial_unknown", "artifact_sha256": projection["artifact_sha256"],
            "buildout_count": len(saved_rows), "site_count": len(site_rows),
            "held_observations": len(projection["projection"]["held"]),
            "buildout_sql": buildout, "site_sql": site}