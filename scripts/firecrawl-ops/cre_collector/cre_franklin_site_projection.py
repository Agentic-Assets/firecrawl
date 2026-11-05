"""Bounded local bridge from immutable Franklin artifact bytes to staged cards."""
import hashlib
import json
import re
import shutil
from pathlib import Path

from cre_franklin_site_sql import stage
from source_evidence.psql_process import run

ROOT = Path(__file__).resolve().parent
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
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
    if result["listings"]:
        stage(result["listings"])
    return {"artifact_sha256": hashlib.sha256(raw).hexdigest(), "projection": result}