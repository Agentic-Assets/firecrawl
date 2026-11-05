"""Bounded local bridge from immutable Franklin artifact bytes to staged cards."""
import hashlib
import json
import re
import shutil
from pathlib import Path

from cre_franklin_site_sql import stage
from source_evidence.psql_process import run

ROOT = Path(__file__).resolve().parent
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_PROJECTION_BYTES = 32 * 1024 * 1024


def project(raw, generation_id, env):
    """Reproject the exact input bytes locally; this is not database admission."""
    if (not isinstance(raw, bytes) or not raw or len(raw) > MAX_ARTIFACT_BYTES
            or not isinstance(generation_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", generation_id)
            or not isinstance(env, dict)):
        raise ValueError("invalid Franklin projection input")
    node = shutil.which("node", path=env.get("PATH"))
    loader = ROOT / "node_modules" / "tsx" / "dist" / "loader.mjs"
    bridge = ROOT / "sources" / "franklin-site-bridge.ts"
    if not node or not loader.is_file() or not bridge.is_file():
        raise ValueError("Franklin projection runtime unavailable")
    output = run([node, "--import", loader.as_uri(), str(bridge), generation_id],
                 raw, env, max_results_bytes=MAX_PROJECTION_BYTES)
    result = json.loads(output)
    if (not isinstance(result, dict) or result.get("coverage") != "partial_unknown"
            or result.get("generationId") != generation_id
            or not isinstance(result.get("listings"), list)
            or not isinstance(result.get("held"), list)
            or not isinstance(result.get("pages"), list)):
        raise ValueError("Franklin projection response invalid")
    original = json.loads(raw)
    if not isinstance(original, dict) or not isinstance(original.get("sources"), list):
        raise TypeError("Franklin original artifact invalid")
    cards = {}
    for source in original["sources"]:
        if not isinstance(source, dict) or source.get("sourceKey") != "franklin-street":
            raise ValueError("Franklin original source invalid")
        tx = source.get("transaction")
        for observation in source.get("retainedObservations") or []:
            if isinstance(observation, dict) and observation.get("schema") == "franklin-street-page-observation-v1":
                if tx in cards or not isinstance(observation.get("cards"), list):
                    raise ValueError("Franklin original card inventory invalid")
                cards[tx] = observation["cards"]

    def restore(tx, reference):
        ordinal = reference.get("ordinal") if isinstance(reference, dict) else None
        if (tx not in cards or type(ordinal) is not int or not 0 <= ordinal < len(cards[tx])):
            raise ValueError("Franklin card reference invalid")
        return cards[tx][ordinal]

    for listing in result["listings"]:
        observation = listing.get("franklinSiteObservation")
        observation["card"] = restore(listing.get("transactionMode"), observation)
    for held in result["held"]:
        reference = held["card"]
        reference["card"] = restore(held["transaction"], reference)
    result["buildout"]["listings"] = original["listings"]
    result["buildout"]["brokers"] = original["brokers"]
    result["buildout"]["pendingListings"] = len(original["listings"])
    result["buildout"]["disposition"] = "private_pending_admission"
    if result["listings"]:
        stage(result["listings"])
    return {"artifact_sha256": hashlib.sha256(raw).hexdigest(), "projection": result}