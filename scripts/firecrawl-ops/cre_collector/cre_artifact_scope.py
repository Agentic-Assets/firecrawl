"""Keep private source observations out of canonical inventory operations."""


def require_canonical_artifact(data, *, allow_matthews_partial_gate=False):
    """Reject declared evidence-only input without promoting its observations.

    A mixed artifact can retain unqualified source context beside accepted
    listing rows; its normal coverage and source gates still apply. This guard
    is a scope boundary, not proof that an unmarked artifact is trustworthy.
    """
    if not isinstance(data, dict):
        return
    meta = data.get("runMeta")
    if not isinstance(meta, dict):
        return
    if "matthewsPartialFill" in meta:
        if not allow_matthews_partial_gate:
            raise ValueError("Matthews partial projection is not a canonical inventory artifact")
        marker = meta["matthewsPartialFill"]
        entries, rows = data.get("sources"), data.get("listings")
        if (not isinstance(marker, dict) or marker.get("schema") != "matthews-partial-gate/v1"
                or marker.get("coverage") != "partial_unknown" or not isinstance(entries, list) or len(entries) != 2
                or not isinstance(rows, list) or not rows or type(data.get("totalListings")) is not int
                or data["totalListings"] != len(rows)
                or {entry.get("transaction") for entry in entries if isinstance(entry, dict)} != {"sale", "lease"}
                or any(not isinstance(entry, dict) or entry.get("sourceKey") != "matthews"
                       or entry.get("truncated") is not True or entry.get("error") != "matthews_partial_incomplete"
                       for entry in entries)
                or any(not isinstance(row, dict) or row.get("sourceKey") != "matthews"
                       or row.get("transactionMode") not in ("sale", "lease") for row in rows)):
            raise ValueError("Matthews partial gate projection invalid")
    if (("terminalCaptureFailure" in meta and meta["terminalCaptureFailure"] is not False)
            or ("evidenceOnly" in meta and meta["evidenceOnly"] is not False)
            or "evidencePurpose" in meta):
        raise ValueError("private source observations are not a canonical inventory artifact")
