"""DAUM fallback identity partition; this module grants no write authority."""

def staged_fallback_members(data):
    """Bind corroborated fallback witnesses to their source listing identities."""
    from cre_daum_current_inventory import SUBSET_SCHEMA_V2, _path_key, _post, subset_source_scope
    from cre_ingest import is_daum_title_fallback

    scope = subset_source_scope(data)
    proofs = [entry["currentInventoryProof"] for entry in data["sources"]]
    if any(p.get("schema") != SUBSET_SCHEMA_V2 for p in proofs):
        raise ValueError("DAUM fallback requires V2 proof")
    witnesses = proofs[0].get("titleFallbackObservations", [])
    if not isinstance(witnesses, list) or any(p.get("titleFallbackObservations", []) != witnesses for p in proofs):
        raise ValueError("DAUM fallback witnesses differ")
    expected = {}
    for item in witnesses:
        if not isinstance(item, dict) or set(item) != {"postId", "cardTitle", "documentTitle"}:
            raise ValueError("DAUM fallback witness shape invalid")
        post, title = item["postId"], item["cardTitle"]
        _post(post)
        if (post in expected or not isinstance(title, str) or not title.strip()
                or title != title.strip() or item["documentTitle"] != f"{title} – Daum Commercial"):
            raise ValueError("DAUM fallback witness invalid")
        expected[post] = item
    rows = {}
    for listing in scope["listings"]:
        if not isinstance(listing, dict):
            raise ValueError("DAUM listing invalid")
        post = listing.get("daumPostId")
        if post not in expected:
            if "daumTitleFallback" in listing:
                raise ValueError("DAUM fallback witness missing")
            continue
        if not is_daum_title_fallback(listing):
            raise ValueError("DAUM fallback listing invalid")
        source_url = listing["url"]
        external_id = _path_key(source_url)
        if listing.get("id") != external_id or listing["daumTitleFallback"] != {
                "cardTitle": expected[post]["cardTitle"], "documentTitle": expected[post]["documentTitle"]}:
            raise ValueError("DAUM fallback source binding differs")
        row = {"native_id": post, "external_id": external_id, "source_url": source_url}
        if post in rows and rows[post] != row:
            raise ValueError("DAUM fallback pass identity differs")
        rows[post] = row
    if set(rows) != set(expected):
        raise ValueError("DAUM fallback listing missing")
    return sorted(rows.values(), key=lambda row: row["external_id"])
