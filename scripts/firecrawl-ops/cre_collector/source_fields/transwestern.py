"""Property-local, source-reported building fields from accepted detail output.

The producer owns feed-to-detail acceptance. These checks bind its scoped
observations to the exact consumed artifact; they do not authenticate a website.
The caller first applies the ordinary strict-artifact freshness validator.
"""

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote


def qualified_building_fields(artifact, artifact_sha256, *, now=None):
    from cre_current_inventory_admission import _time

    if not isinstance(artifact_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", artifact_sha256
    ):
        return {}
    if not isinstance(artifact, dict) or not isinstance(artifact.get("runMeta"), dict):
        return {}
    meta = artifact["runMeta"]
    if meta.get("mode") not in ("full", "enrich"):
        return {}
    fresh = meta.get("freshness")
    if not isinstance(fresh, dict) or fresh.get("requireFreshDetails") is not True:
        return {}
    try:
        start, end = _time(fresh["generationStartedAt"]), _time(meta["finishedAt"])
        current = now or datetime.now(UTC)
        if (
            not current - timedelta(hours=24)
            <= start
            <= end
            <= current + timedelta(minutes=5)
        ):
            return {}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {}
    result = {}
    for listing in artifact.get("listings") or []:
        if not isinstance(listing, dict) or listing.get("sourceKey") != "transwestern":
            continue
        fields = _listing_fields(listing, fresh, artifact_sha256, start, end)
        if fields:
            result[id(listing)] = fields
    return result


def _listing_fields(listing, fresh, artifact_sha256, start, end):
    from cre_current_inventory_admission import _time

    if listing.get("detailError") or listing.get("inventoryOnly") is not None:
        return {}
    feed = listing.get("rawTranswesternFeed")
    provenance = listing.get("freshnessProvenance")
    facts = listing.get("transwesternBuildingFacts")
    if not all(isinstance(value, dict) for value in (feed, provenance, facts)):
        return {}
    slug = feed.get("PageUrl")
    if (
        not isinstance(slug, str)
        or not slug.strip()
        or listing.get("id") != slug.strip()
    ):
        return {}
    slug = slug.strip()
    url = "https://transwestern.com/property/" + quote(slug, safe="/~()*!'")
    if (
        listing.get("url") != url
        or facts.get("sourceUrl") != url
        or facts.get("schema") != "transwestern-building-facts-v1"
        or facts.get("scope") != "property_details_tab"
        or facts.get("state")
        not in (
            "inspected",
            "ambiguous_fields",
            "malformed_or_concealed_row",
            "ambiguous_details_structure",
        )
        or facts.get("visibility") != "not_evaluated"
        or not isinstance(facts.get("bodySha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", facts["bodySha256"])
        or provenance.get("detailScope") != "detail_page"
        or provenance.get("method") != "transwestern_detail_page"
        or provenance.get("cacheDisposition") != "live"
        or not isinstance(fresh.get("generationId"), str)
        or not fresh["generationId"]
        or provenance.get("generationId") != fresh["generationId"]
        or facts.get("observedAt") != listing.get("detailObservedAt")
    ):
        return {}
    try:
        observed = _time(listing["detailObservedAt"])
        inventory = _time(listing["inventoryObservedAt"])
        if not start <= inventory <= observed <= end:
            return {}
        if slug == "-":
            published = listing.get("transwesternPublishedIdentity")
            if (
                not isinstance(published, dict)
                or published.get("url") != url
                or published.get("sitemapUrl") != "https://transwestern.com/sitemap.xml"
                or published.get("generationId") != fresh["generationId"]
                or not isinstance(published.get("identitySha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", published["identitySha256"])
                or not inventory <= _time(published["verifiedAt"]) <= observed
            ):
                return {}
    except (ValueError, TypeError, KeyError, OverflowError):
        return {}
    if facts["state"] == "ambiguous_details_structure":
        return {"values": {}, "blocked": ["elevator_count", "typical_floor_size_text"]}
    observations = facts.get("observations")
    if not isinstance(observations, list) or len(observations) > 200:
        return {}
    if any(not isinstance(item, dict) for item in observations):
        return {}
    fields = {}
    blocked = set()
    for source_field, column, scalar in (
        ("elevators", "elevator_count", "elevatorCount"),
        ("typical_floor_size", "typical_floor_size_text", "typicalFloorSizeText"),
    ):
        matches = [
            (ordinal, item)
            for ordinal, item in enumerate(observations)
            if item.get("field") == source_field
        ]
        if len(matches) > 1:
            blocked.add(column)
        if len(matches) != 1:
            continue
        ordinal, item = matches[0]
        literal, label = item.get("literal"), item.get("label")
        if (
            item.get("eligibility") != "eligible"
            or not isinstance(literal, str)
            or not 1 <= len(literal) <= 2048
            or "\x00" in literal
            or not isinstance(label, str)
        ):
            blocked.add(column)
            continue
        label_pattern = (
            r"elevators?" if source_field == "elevators" else r"typical\s*floor\s*size"
        )
        if not re.fullmatch(label_pattern, label, flags=re.IGNORECASE):
            blocked.add(column)
            continue
        if source_field == "elevators":
            if not re.fullmatch(r"0|[1-9][0-9]{0,9}", literal):
                blocked.add(column)
                continue
            value = int(literal)
            if (
                value > 2_147_483_647
                or type(facts.get(scalar)) is not int
                or facts[scalar] != value
            ):
                blocked.add(column)
                continue
        else:
            value = literal
            if not re.search(r"[0-9]", literal) or facts.get(scalar) != value:
                blocked.add(column)
                continue
        fields[column] = {
            "schema_version": "cre-source-field-observation/v1",
            "field": column,
            "source_key": "transwestern",
            "native_id": slug,
            "url": url,
            "tenure": "property",
            "source_label": label,
            "observation_ordinal": ordinal,
            "literal": literal,
            "original_observed_at": listing["detailObservedAt"],
            "observed_at": observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "generation_id": fresh["generationId"],
            "artifact_sha256": artifact_sha256,
            "body_sha256": facts["bodySha256"],
            "source_scope": facts["scope"],
            "visibility": facts["visibility"],
            "value": value,
        }
    # Missing is no opinion; an explicit unresolved observation must survive
    # until merge so a second pass cannot silently erase a known conflict.
    return {"values": fields, "blocked": sorted(blocked)} if fields or blocked else {}
