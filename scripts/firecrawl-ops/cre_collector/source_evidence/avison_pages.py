"""Validate private Avison producer files and project before database planning."""
import base64
import binascii
import hashlib
import json

from buildout_observation_artifact import invalid_number, unique_object

from source_evidence import acquisition, avison_native, avison_response, events

FORMAT = "avison_sharplaunch_website_v1"
MAX_ENTRY_BYTES = ((16 * 1024 * 1024 * 4 + 2) // 3) + 16384
PROVENANCE = {"bodyKind": "collector_decoded_text_reencoded_utf8_not_fetch_or_wire_bytes",
              "identity": "unverified", "visibility": "unverified", "completeness": "unverified"}


def decode_entry(raw, filename, binding, *, native_format=FORMAT):
    if native_format not in (FORMAT, "avison_sharplaunch_website_retained_v2"):
        raise ValueError("unsupported Avison native format")
    if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY_BYTES:
        raise ValueError("Avison native page exceeds bounds")
    entry = json.loads(raw.decode("utf8"), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (not isinstance(entry, dict) or set(entry) != {"schema", "receipt", "responseBodyBase64", "provenance"}
            or entry["schema"] != "cre-avison-sharplaunch-website-page/v1" or entry["provenance"] != PROVENANCE
            or filename != f"page-website-{hashlib.sha256(raw).hexdigest()}.json"):
        raise ValueError("Avison native page contract mismatch")
    avison_response.receipt_checked(entry["receipt"])
    try:
        body = base64.b64decode(entry["responseBodyBase64"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("Avison native response encoding invalid") from exc
    if base64.b64encode(body).decode() != entry["responseBodyBase64"] or len(body) > events.MAX_ARTIFACT_BYTES:
        raise ValueError("Avison native response exceeds bounds")
    try:
        document, literals = acquisition.decode_json(body)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None, "invalid_json"
    encoding = "items_array"
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        # Preserve accessible JSON without pretending to recognize its inventory.
        document = {"items": [], "unmappedResponse": document}
        literals = {"/unmappedResponse" + path: value for path, value in literals.items()}
        encoding = "wrapped_unmapped_json"
    if native_format == FORMAT:
        projected, projected_literals, projection = avison_native.project(document, literals)
    else:
        projected, projected_literals = document, literals
        projection = {"version": avison_response.RETAIN_VERSION, "changes": [], "unresolvedProse": 0}
    observation = avison_response.convert(projected, projected_literals, projection,
                                         receipt=entry["receipt"], capture_context=binding, response_encoding=encoding)
    return observation, None
