"""Source-local consistency proof for staged Transwestern image roles.

This does not authorize deletion or prove source freshness. Callers must bind
the artifact/code and compare the actual retained-parent preimage separately.
"""

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit


def _url(value):
    # Lazy import keeps a future ingest hook free of a module-import cycle.
    from cre_ingest import http_url_or_none

    if not isinstance(value, str) or http_url_or_none(value) != value:
        raise ValueError("invalid Transwestern role URL")
    return value


def _leaves(value):
    pending = [value]
    while pending:
        leaf = pending.pop()
        if not isinstance(leaf, dict):
            raise ValueError("invalid Transwestern raw payload")
        if "sourceKey" in leaf:
            if leaf["sourceKey"] != "transwestern":
                raise ValueError("foreign Transwestern role source")
            yield leaf
        elif set(leaf) == {"primary", "secondary_pass"}:
            pending.extend([leaf["secondary_pass"], leaf["primary"]])
        else:
            raise ValueError("invalid Transwestern duplicate-pass wrapper")


def _template(url):
    parsed = urlsplit(url)
    path = parsed.path
    if parsed.hostname == "images.transwestern.com":
        path = re.sub(r"^/f[0-9]+/transwestern\.com(?=/assets/images/)", "", path)
    elif parsed.hostname != "transwestern.com":
        return False
    return re.fullmatch(
        r"/assets/images/(?:tw_gl\.png|connect-image\.png|transwestern-mapmarker\.png|mail-3\.png)",
        path,
    ) is not None


def _leaf_proof(leaf, row):
    if (
        not isinstance(leaf.get("id"), str)
        or leaf["id"].strip() != row["external_id"]
        or leaf.get("url") != row["source_url"]
        or leaf.get("canonicalUrl") != row["canonical_url"]
        or leaf.get("transwesternPhotoRoleVersion") != "v1"
        or leaf.get("detailError") is not None
    ):
        raise ValueError("Transwestern role identity/version/detail mismatch")
    try:
        observed = datetime.fromisoformat(leaf["detailObservedAt"].replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("missing timezone")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("Transwestern role detail observation missing") from exc
    provenance = leaf.get("freshnessProvenance")
    if not isinstance(provenance, dict) or (
        provenance.get("method"), provenance.get("cacheDisposition")
    ) != ("transwestern_detail_page", "live"):
        raise ValueError("Transwestern role detail provenance missing")
    photos = leaf.get("photos", [])
    if not isinstance(photos, list):
        raise ValueError("invalid Transwestern leaf photos")
    photos = [_url(url) for url in photos]
    evidence = leaf.get("transwesternExcludedPhotoEvidence")
    entries = []
    if evidence is not None:
        if (
            not isinstance(evidence, dict)
            or set(evidence) != {"schema", "entries"}
            or evidence["schema"] != "transwestern-photo-role-v1"
            or not isinstance(evidence["entries"], list)
        ):
            raise ValueError("invalid Transwestern exclusion schema")
        for entry in evidence["entries"]:
            if not isinstance(entry, dict) or set(entry) != {"url", "role", "basis"}:
                raise ValueError("invalid Transwestern exclusion entry")
            url = _url(entry["url"])
            pair = (entry["role"], entry["basis"])
            if pair == ("template_resource", "existing_source_template_policy"):
                valid = _template(url)
            elif pair == ("broker_portrait", "parsed_contact_avatar"):
                contacts = leaf.get("contactsDetailed", [])
                valid = isinstance(contacts, list) and any(
                    isinstance(contact, dict)
                    and contact.get("avatarUrl") == url
                    and any(isinstance(contact.get(key), str) and contact[key].strip()
                            for key in ("name", "profileUrl", "email", "phone"))
                    for contact in contacts
                )
            else:
                valid = False
            if not valid or url in photos:
                raise ValueError("unproved or retained Transwestern exclusion")
            entries.append(dict(entry))
    primary = leaf.get("transwesternPrimaryPhotoEvidence")
    if primary is not None:
        if (
            not isinstance(primary, dict)
            or set(primary) != {"url", "basis"}
            or primary["basis"] not in ("provider_feed", "property_gallery")
            or not photos
            or primary["url"] != photos[0]
        ):
            raise ValueError("invalid Transwestern primary witness")
        if primary["basis"] == "provider_feed":
            feed = leaf.get("rawTranswesternFeed")
            feed_image = feed.get("PropertyImage") if isinstance(feed, dict) else None
            if not isinstance(feed_image, str) or _url(urljoin(
                "https://transwestern.com", " ".join(feed_image.split())
            )) != primary["url"]:
                raise ValueError("Transwestern primary feed reference mismatch")
        primary = dict(primary)
    return photos, entries, primary


def role_proof(row):
    """Validate all owned raw passes and return only final staged role evidence."""
    if (
        not isinstance(row, dict)
        or row.get("slug") != "transwestern"
        or not isinstance(row.get("external_id"), str)
        or not row["external_id"]
        or row["external_id"].strip() != row["external_id"]
    ):
        raise ValueError("invalid canonical Transwestern staged identity")
    for field in ("source_url", "canonical_url"):
        parsed = urlsplit(_url(row.get(field)))
        if (
            parsed.hostname not in {"transwestern.com", "www.transwestern.com"}
            or not parsed.path.startswith("/property/")
            or not parsed.path[len("/property/"):].strip("/")
        ):
            raise ValueError("foreign Transwestern detail URL scope")
    images = row.get("images")
    if not isinstance(images, list) or any(
        not isinstance(image, dict) or type(image.get("isPrimary")) is not bool
        for image in images
    ):
        raise ValueError("invalid Transwestern staged images")
    retained = {_url(image.get("url")) for image in images}
    primaries = [image["url"] for image in images if image["isPrimary"]]
    if len(primaries) != (1 if images else 0):
        raise ValueError("ambiguous Transwestern staged primary")
    leaf_photos = set()
    excluded = {}
    selected = None
    for leaf in _leaves(row.get("raw_data")):
        photos, entries, primary = _leaf_proof(leaf, row)
        leaf_photos.update(photos)
        if primary and primaries == [primary["url"]] and selected is None:
            selected = primary
        for entry in entries:
            if entry["url"] in retained:
                continue
            prior = excluded.get(entry["url"])
            if prior is not None and prior != entry:
                raise ValueError("conflicting Transwestern exclusion roles")
            excluded[entry["url"]] = entry
    if retained != leaf_photos:
        raise ValueError("Transwestern staged/leaf photo membership differs")
    return {"excluded": list(excluded.values()), "primary": selected}
