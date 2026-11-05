"""Build a private source-qualified Franklin partial artifact from exact input bytes."""
import copy
import json

from cre_franklin_site_projection import project


def derive(raw, binding, env):
    """Return the site subset and account for original Buildout peers as pending.

    The owner must retain the original bytes, bind this derived output and
    independently authorize its partial claims before passing it to ingest.
    """
    projected = project(raw, binding["generation_id"], env)
    if projected["artifact_sha256"] != binding["artifact_sha256"]:
        raise ValueError("Franklin original artifact digest differs")
    original = json.loads(raw)
    meta = original.get("runMeta") or {}
    fresh = meta.get("freshness") or {}
    if (meta.get("mode") != "full" or meta.get("transactions") != ["sale", "lease"]
            or meta.get("maxItemsPerSource") is not None or meta.get("terminalCaptureFailure")
            or meta.get("startedAt") != binding["started_at"] or meta.get("finishedAt") != binding["finished_at"]
            or fresh.get("generationId") != binding["generation_id"]
            or fresh.get("generationStartedAt") != binding["generation_started_at"]
            or fresh.get("requireFreshDetails") is not True):
        raise ValueError("Franklin original scope differs")
    entries = original["sources"]
    if len(entries) != 2 or {entry.get("transaction") for entry in entries} != {"sale", "lease"}:
        raise ValueError("Franklin original transaction scope differs")
    subset = projected["projection"]
    if not subset["listings"]:
        raise ValueError("Franklin has no identifiable writable site cards")
    listings = copy.deepcopy(subset["listings"])
    for listing in listings:
        observation = listing["franklinSiteObservation"]
        listing["inventoryObservedAt"] = observation["observedAt"]
        listing["freshnessProvenance"] = {
            "generationId": binding["generation_id"],
            "method": "franklin_site_card",
            "cacheDisposition": "live",
            "detailScope": "partial_site_card",
        }
    sources = []
    for original_entry in entries:
        tx = original_entry["transaction"]
        count = sum(row["transactionMode"] == tx for row in listings)
        sources.append({
            "sourceKey": "franklin-street", "transaction": tx, "supported": True,
            "truncated": True, "listingsCollected": count,
            "error": "source_incomplete" if original_entry.get("error") else "buildout_peers_pending",
        })
    derived_meta = copy.deepcopy(meta)
    derived_meta.pop("evidenceOnly", None)
    derived_meta.pop("evidencePurpose", None)
    derived_meta.pop("franklinSidecar", None)
    derived_meta["franklinSitePartial"] = {
        "schema": "franklin-site-partial-v1",
        "sourceArtifactSha256": binding["artifact_sha256"],
        "coverage": "partial_unknown",
        "buildoutPending": subset["buildout"]["pendingListings"],
        "heldCardObservations": len(subset["held"]),
        "identityUncertainty": subset["identityUncertainty"],
    }
    return {"runMeta": derived_meta, "sources": sources, "listings": listings,
            "brokers": [], "totalListings": len(listings)}