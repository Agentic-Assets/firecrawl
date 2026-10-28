"""Keep private source observations out of canonical inventory operations."""


def require_canonical_artifact(data):
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
    if (("evidenceOnly" in meta and meta["evidenceOnly"] is not False)
            or "evidencePurpose" in meta):
        raise ValueError("private source observations are not a canonical inventory artifact")
