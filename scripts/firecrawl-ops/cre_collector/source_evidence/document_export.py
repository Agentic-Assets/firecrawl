"""Reconstruct and privately export a complete retained document capture."""
import copy
from datetime import datetime, timezone

from source_evidence import events, private_export

FIELDS = frozenset(events.EVENT_FIELDS)
MAX_ROWS = 71


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("document event time is invalid")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("document event time has no timezone")
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError("document event time is invalid") from exc


def reconstruct(rows):
    """Verify complete ordered source rows and return text plus unverified metadata."""
    if not isinstance(rows, list) or not 2 <= len(rows) <= MAX_ROWS:
        raise ValueError("document capture row count is invalid")
    if any(not isinstance(row, dict) or set(row) != FIELDS for row in rows):
        raise ValueError("document event fields are invalid")
    if any(type(row["item_no"]) is not int or row["item_no"] != index
           for index, row in enumerate(rows)):
        raise ValueError("document event ordinals are incomplete or unordered")
    first = rows[0]
    facts = first["facts"]
    if (not isinstance(facts, list) or len(facts) != 1 or not isinstance(facts[0], dict)
            or facts[0].get("kind") != "document_outcome" or not isinstance(facts[0].get("value"), dict)):
        raise ValueError("document outcome header is missing")
    header = copy.deepcopy(facts[0]["value"])
    source, extraction = header.get("source"), header.get("extraction")
    if (not isinstance(source, dict) or not isinstance(extraction, dict)
            or extraction.get("status") != "extracted" or "text" in extraction):
        raise ValueError("document capture does not contain extracted text")
    source_clock = events.timestamp(source.get("observed_at"))
    source_instant = _instant(source_clock)
    pieces = []
    total = 0
    for row in rows[1:]:
        chunk = row["extracted_text"]
        if not isinstance(chunk, str):
            raise ValueError("document chunk is missing")
        total += len(chunk.encode("utf-8"))
        if total > events.MAX_TEXT_BYTES:
            raise ValueError("document text exceeds bound")
        pieces.append(chunk)
    text = "".join(pieces)
    if extraction.get("text_sha256") != events.sha256(text.encode("utf-8")):
        raise ValueError("document text digest mismatch")
    artifact = {**header, "extraction": {**extraction, "text": text}}
    expected = events.plan(artifact, source_key=first["source_key"], external_id=first["external_id"])
    events.validate_plan(expected)
    normalized = copy.deepcopy(rows)
    for row in normalized:
        if _instant(row["observed_at"]) != source_instant:
            raise ValueError("document event observation times disagree")
        row["observed_at"] = source_clock
    if events.encoded(normalized) != events.encoded(expected["events"]):
        raise ValueError("document events disagree with reconstructed artifact")
    metadata = {"schema_version": "cre-source-evidence-document-export/v1",
                "capture_id": first["capture_id"], "source_key": first["source_key"],
                "external_id": first["external_id"], "document": header,
                "provenance": copy.deepcopy(first["provenance"]),
                "text_sha256": extraction["text_sha256"], "text_bytes": total,
                "chunk_count": len(rows) - 1,
                "coverage": "unknown", "publisher_identity": "unverified",
                "chunk_boundaries": "utf8_byte_windows_not_physical_pages"}
    return text, metadata


def export(rows, outdir):
    """Create a new private directory containing exact UTF-8 text and metadata."""
    text, metadata = reconstruct(rows)
    text_format = metadata["document"]["extraction"].get("text_format", "markdown")
    filename = {"markdown": "document.md", "plain": "document.txt"}.get(text_format)
    if filename is None:
        raise ValueError("document export text format is unsupported")
    metadata["text_file"] = filename
    private_export.write(outdir, [(filename, text.encode("utf-8")),
                                  ("metadata.json", events.encoded(metadata) + b"\n")])
    return metadata
