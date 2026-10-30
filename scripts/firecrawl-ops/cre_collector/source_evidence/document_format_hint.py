"""Derive bounded, unverified CSV assertions from a selected source candidate."""

import re

from source_evidence import events

SCHEMA = "cre-document-format-hint/v1"
_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]{1,180}\.([a-z0-9]{1,12})$", re.IGNORECASE)


def derive(selected, item):
    """Keep provenance in the candidate; return no hint when none was asserted."""
    assertions = []
    for index, occurrence in enumerate(item["candidate"]["occurrences"]):
        labels = occurrence.get("labels", {})
        for field in ("type", "download", "name", "title"):
            value = labels.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            value = value.strip()
            if field == "type":
                fmt = "csv" if value.lower() == "text/csv" else "unsupported"
            else:
                match = _FILENAME.fullmatch(value)
                if not match:
                    continue
                fmt = "csv" if match.group(1).lower() == "csv" else "unsupported"
            assertions.append({"occurrence": index, "field": field, "format": fmt})
    if not assertions:
        return None
    count = len(assertions)
    assertions_sha256 = events.sha256(events.encoded(assertions))
    status = "resolved" if count <= 64 and all(a["format"] == "csv" for a in assertions) else "unresolved"
    return {"schema": SCHEMA, "status": status, "format": "csv" if status == "resolved" else None,
            "candidate_sha256": item["candidate_sha256"],
            "selection_sha256": events.sha256(events.encoded(selected)), "assertions": assertions[:64],
            "assertion_count": count, "assertions_sha256": assertions_sha256,
            "omitted_assertions": max(0, count - 64),
            "authority": "source_asserted_unverified"}
