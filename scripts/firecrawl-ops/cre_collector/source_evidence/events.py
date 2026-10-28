"""Convert retained artifacts into immutable, explicitly unverified events."""
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlsplit

CHUNK_BYTES = 60_000
MAX_TEXT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
EVENT_FIELDS = ("capture_id", "item_no", "source_key", "external_id", "observed_at", "source_url",
                "provenance", "facts", "document_url", "extractor_version", "extracted_text", "text_sha256")
CREDENTIAL_QUERY_KEYS = {"token", "access_token", "api_key", "apikey", "authorization", "auth",
                         "signature", "sig", "x-amz-signature", "x-amz-credential",
                         "x-goog-signature", "x-goog-credential", "client_secret"}
URL_TOKEN = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z", value):
        raise ValueError("observation time must be UTC RFC3339")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def http_url(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode("utf-8")) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid source URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source URL must be HTTP(S), without credentials")
    if any(key.lower() in CREDENTIAL_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
        raise ValueError("credential-bearing source URL requires reviewed redaction")
    return value


def split_text(text):
    """Split without dropping bytes; chunks are not claims of physical pages."""
    raw = text.encode("utf-8")
    if len(raw) > MAX_TEXT_BYTES:
        raise ValueError("document text exceeds limit")
    chunks = []
    while raw:
        end = min(CHUNK_BYTES, len(raw))
        while True:
            try:
                chunks.append(raw[:end].decode("utf-8"))
                break
            except UnicodeDecodeError:
                end -= 1
        raw = raw[end:]
    return chunks


def _event(capture, ordinal, source_key, observed_at, url, provenance, facts, external_id):
    return {"capture_id": capture, "item_no": ordinal, "source_key": source_key,
            "external_id": external_id, "observed_at": observed_at, "source_url": url,
            "provenance": provenance, "facts": facts, "document_url": None,
            "extractor_version": None, "extracted_text": None, "text_sha256": None}


def plan(artifact, *, source_key, observed_at=None, external_id=None):
    if not isinstance(artifact, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", source_key):
        raise ValueError("invalid artifact or asserted source key")
    if external_id is not None and (not isinstance(external_id, str) or not 0 < len(external_id.encode()) <= 1024):
        raise ValueError("invalid asserted external identity")
    raw = encoded(artifact)
    if len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("artifact exceeds limit")
    artifact_hash = sha256(raw)
    if artifact.get("schema_version") == "cre-document-text/v1":
        capture_clock = timestamp(artifact.get("source", {}).get("observed_at"))
    elif artifact.get("schema") in ("cre-retained-observations/v1", "cre-retained-observations/v2"):
        capture_clock = timestamp(artifact.get("provenance", {}).get("observedAt"))
        if observed_at is not None and timestamp(observed_at) != capture_clock:
            raise ValueError("observation clock mismatch")
    elif artifact.get("schema") in ("cre-json-response-observation/v1", "cre-json-response-context-observation/v1",
                                   "cre-avison-native-observation/v1"):
        capture_clock = timestamp(artifact.get("receipt", {}).get("finishedAt"))
        if observed_at is not None and timestamp(observed_at) != capture_clock:
            raise ValueError("observation clock mismatch")
    elif artifact.get("schema") == "cre-buildout-parsed-observation/v1":
        capture_clock = timestamp(artifact.get("receipt", {}).get("capturedAt"))
        if observed_at is not None and timestamp(observed_at) != capture_clock:
            raise ValueError("observation clock mismatch")
    else:
        capture_clock = timestamp(observed_at)
    capture = "evidence-" + sha256(encoded([source_key, external_id, artifact_hash, capture_clock]))
    provenance = {"source_identity": "unverified", "visibility": "unverified", "promotion": "not_authorized",
                  "artifact_sha256": artifact_hash, "artifact_hash_kind": "canonical_json_utf8",
                  "original_retention": "not_in_this_store", "source_key_origin": "caller_asserted",
                  "observed_at_origin": "caller_asserted", "freshness": "unverified",
                  "original_sha256_verification": "artifact_claim_not_database_reverified"}
    if artifact.get("schema_version") == "cre-document-text/v1":
        source, extraction, parser = artifact.get("source"), artifact.get("extraction"), artifact.get("parser")
        if not all(isinstance(v, dict) for v in (source, extraction, parser)):
            raise ValueError("invalid document artifact")
        clock = timestamp(source.get("observed_at"))
        url = http_url(source.get("url"))
        if url is None:
            raise ValueError("document source URL required")
        state = extraction.get("status")
        if state not in ("extracted", "ocr_needed", "failed") or extraction.get("coverage") != "unknown":
            raise ValueError("unsupported extraction state or coverage claim")
        text = extraction.get("text")
        if state == "extracted":
            if not isinstance(text, str) or not text.strip() or extraction.get("text_sha256") != sha256(text.encode("utf-8")):
                raise ValueError("extracted text digest mismatch")
            chunks = split_text(text)
        else:
            if text is not None or extraction.get("text_sha256") is not None:
                raise ValueError("failed extraction cannot claim text")
            chunks = []
        metadata = {**artifact, "extraction": {k: v for k, v in extraction.items() if k != "text"}}
        provenance = {**provenance, "capture_kind": "document_text", "coverage": "unknown",
                      "text_sha256": extraction.get("text_sha256"), "text_chunks": len(chunks),
                      "chunking": "utf8_byte_windows_not_physical_pages"}
        events = [_event(capture, 0, source_key, clock, url, provenance,
                         [{"kind": "document_outcome", "value": metadata}], external_id)]
        for number, chunk in enumerate(chunks, 1):
            event = _event(capture, number, source_key, clock, url,
                           {**provenance, "chunk_ordinal": number}, [], external_id)
            event.update(document_url=url, extractor_version=str(parser.get("name")) + "@" + str(parser.get("version")),
                         extracted_text=chunk, text_sha256=sha256(chunk.encode("utf-8")))
            events.append(event)
    elif artifact.get("schema") in ("cre-retained-observations/v1", "cre-retained-observations/v2"):
        from source_evidence import collector
        if external_id is not None:
            raise ValueError("capture-wide evidence cannot claim a listing identity")
        original = collector.reconstruct(artifact)
        rows = artifact["records"]
        policy_binding = artifact.get("provenance", {}).get("automaticPolicy")
        numeric_literals = artifact.get("provenance", {}).get("numericLiterals")
        rebuilt = collector.convert(original, source_key=source_key,
                                    policy_binding=policy_binding, numeric_literals=numeric_literals,
                                    projection=artifact.get("provenance", {}).get("projection"),
                                    schema=artifact["schema"],
                                    integral_floats=artifact.get("provenance", {}).get("integralFloats"))
        if encoded(rebuilt) != raw:
            raise ValueError("retained observations disagree with collector artifact")
        provenance = {**provenance, "capture_kind": "collector_emitted_listings", "coverage": "unverified",
                      "observed_at_origin": "collector_run_finished_at"}
        version2 = artifact["schema"] == collector.SCHEMA_V2
        if version2:
            provenance = {**provenance, "capture_kind": "collector_records_and_unqualified_observations"}
        header = {k: v for k, v in artifact.items() if k not in ("records", "observations")}
        events = [_event(capture, 0, source_key, capture_clock, None, provenance,
                         [{"kind": "retained_capture_context_v2" if version2 else "retained_capture_context",
                           "value": header}], None)]
        events.extend(_event(capture, n, source_key, capture_clock, None, provenance,
                             [{"kind": "source_record", "value": row}], None) for n, row in enumerate(rows, 1))
        if version2:
            events.extend(_event(capture, len(rows) + n, source_key, capture_clock, None, provenance,
                                 [{"kind": "source_observation", "value": row}], None)
                          for n, row in enumerate(artifact["observations"], 1))
    elif artifact.get("schema") in ("cre-json-response-observation/v1", "cre-json-response-context-observation/v1"):
        from source_evidence import acquisition
        if external_id is not None:
            raise ValueError("capture-wide response cannot claim a listing identity")
        acquisition.validate(artifact)
        if artifact["receipt"]["sourceKey"] != source_key:
            raise ValueError("source assertion disagrees with response")
        provenance = {**provenance, "capture_kind": "json_response", "coverage": "unverified",
                      "observed_at_origin": "caller_asserted_response_finished_at",
                      "numeric_encoding": "json_pointer_to_literal_null_placeholder_v1"}
        header = {k: v for k, v in artifact.items() if k != "records"}
        url = http_url(artifact["receipt"]["finalUrl"])
        events = [_event(capture, 0, source_key, capture_clock, url, provenance,
                         [{"kind": "json_response_context", "value": header}], None)]
        events.extend(_event(capture, n, source_key, capture_clock, url, provenance,
                             [{"kind": "source_record", "value": row}], None)
                      for n, row in enumerate(artifact["records"], 1))
    elif artifact.get("schema") == "cre-avison-native-observation/v1":
        from source_evidence import avison_response
        if source_key != "avison-young" or external_id is not None:
            raise ValueError("Avison native observation requires its source identity")
        avison_response.validate(artifact)
        provenance = {**provenance, "capture_kind": "source_projected_json_response", "coverage": "unverified",
                      "observed_at_origin": "collector_asserted_response_finished_at",
                      "original_sha256_verification": "original_not_reconstructable_after_projection"}
        header = {k: v for k, v in artifact.items() if k != "records"}
        url = http_url(artifact["receipt"]["requestUrl"])
        events = [_event(capture, 0, source_key, capture_clock, url, provenance,
                         [{"kind": "avison_native_context", "value": header}], None)]
        events.extend(_event(capture, n, source_key, capture_clock, url, provenance,
                             [{"kind": "source_record", "value": row}], None)
                      for n, row in enumerate(artifact["records"], 1))
    elif artifact.get("schema") == "cre-buildout-parsed-observation/v1":
        from source_evidence import buildout_response
        if external_id is not None or artifact.get("receipt", {}).get("sourceKey") != source_key:
            raise ValueError("parsed Buildout page requires its capture-wide source scope")
        buildout_response.validate(artifact)
        provenance = {**provenance, "capture_kind": "collector_parsed_buildout_page", "coverage": "unverified",
                      "observed_at_origin": "collector_parsed_page_capture_time",
                      "numeric_encoding": "json_pointer_to_literal_null_placeholder_v1"}
        header = {k: v for k, v in artifact.items() if k != "records"}
        events = [_event(capture, 0, source_key, capture_clock, None, provenance,
                         [{"kind": "buildout_parsed_context", "value": header}], None)]
        events.extend(_event(capture, n, source_key, capture_clock, None, provenance,
                             [{"kind": "source_record", "value": row}], None)
                      for n, row in enumerate(artifact["records"], 1))
    elif artifact.get("schema") == "buildout-private-observation-artifact-v1":
        if external_id is not None:
            raise ValueError("capture-wide Buildout evidence cannot claim a listing identity")
        clock = timestamp(observed_at)
        original_provenance = artifact.get("provenance", {})
        if original_provenance.get("sourceKey") != source_key:
            raise ValueError("source assertion disagrees with retained artifact")
        rows = artifact.get("records")
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("invalid observation records")
        provenance = {**provenance, "capture_kind": "buildout_inventory", "coverage": "unverified"}
        header = {k: v for k, v in artifact.items() if k != "records"}
        events = [_event(capture, 0, source_key, clock, None, provenance,
                         [{"kind": "capture_context", "value": header}], None)]
        events.extend(_event(capture, n, source_key, clock, None, provenance,
                             [{"kind": "source_record", "value": row}], None) for n, row in enumerate(rows, 1))
    else:
        raise ValueError("unsupported retained artifact")
    for event in events:
        if len(encoded(event["provenance"])) > 12000 or len(encoded(event["facts"])) > 3_500_000:
            raise ValueError("event metadata exceeds storage contract")
    result = {"schema_version": "cre-source-evidence-plan/v1", "events": events,
              "source_key": source_key, "artifact_sha256": artifact_hash}
    result["plan_sha256"] = sha256(encoded(result))
    return result


def validate_plan(value):
    """Reconstruct the artifact and derive every row again, rather than trusting its hash."""
    if (not isinstance(value, dict) or value.get("schema_version") != "cre-source-evidence-plan/v1"
            or value.get("plan_sha256") != sha256(encoded({k: v for k, v in value.items() if k != "plan_sha256"}))):
        raise ValueError("evidence plan digest mismatch")
    rows = value.get("events")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10001:
        raise ValueError("invalid evidence rows")
    first = rows[0]
    try:
        kind = first["facts"][0]["kind"]
        metadata = first["facts"][0]["value"]
        if kind == "document_outcome":
            artifact = {**metadata, "extraction": {**metadata["extraction"]}}
            artifact["extraction"]["text"] = ("".join(row["extracted_text"] for row in rows[1:])
                                                if metadata["extraction"]["status"] == "extracted" else None)
            expected = plan(artifact, source_key=value["source_key"], external_id=first["external_id"])
        elif kind == "capture_context":
            artifact = {**metadata, "records": [row["facts"][0]["value"] for row in rows[1:]]}
            expected = plan(artifact, source_key=value["source_key"], observed_at=first["observed_at"])
        elif kind in ("json_response_context", "avison_native_context", "buildout_parsed_context"):
            artifact = {**metadata, "records": [row["facts"][0]["value"] for row in rows[1:]]}
            expected = plan(artifact, source_key=value["source_key"])
        elif kind in ("retained_capture_context", "retained_capture_context_v2"):
            from source_evidence import collector
            expected = plan(collector.from_events(rows), source_key=value["source_key"])
        else:
            raise ValueError("unsupported evidence kind")
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("malformed evidence plan") from exc
    if encoded(value) != encoded(expected):
        raise ValueError("evidence rows disagree with reconstructed artifact")


def validate_admission(plan_value, admission):
    """An operator receipt permits private storage; it does not authenticate a publisher."""
    validate_plan(plan_value)
    if (not isinstance(admission, dict) or admission.get("schema_version") != "cre-source-evidence-admission/v1"
            or admission.get("plan_sha256") != plan_value.get("plan_sha256")
            or admission.get("purpose") != "private_unverified_evidence"
            or admission.get("data_scope") != "public_listing_source"
            or admission.get("content_policy") != "reviewed_for_private_retention"
            or admission.get("source_keys") != [plan_value.get("source_key")]):
        raise ValueError("missing or mismatched private-retention admission")
    validate_content_policy(plan_value)


def validate_content_policy(plan_value):
    """Apply source and content restrictions independent of authorization form."""
    validate_plan(plan_value)
    # Existing JLL controls prohibit re-exposing withheld price-linked content.
    # A general artifact receipt cannot replace that source-specific policy.
    if plan_value["source_key"] in ("jll", "jll-main", "jll-investor"):
        raise ValueError("JLL requires its source-specific withheld-content adapter")
    if plan_value["source_key"] == "avison-young":
        from source_evidence.avison import validate_records
        validate_records(plan_value)
    document_text = None
    if plan_value["events"][0]["facts"][0]["kind"] == "document_outcome":
        # validate_plan already reconstructed and checked the complete bounded
        # document. Scan across chunk boundaries as well as metadata.
        document_text = "".join(row["extracted_text"] for row in plan_value["events"][1:])
    for row in plan_value["events"]:
        pending = [row]
        if row["item_no"] == 0 and document_text is not None:
            pending.append(document_text)
        inspected = 0
        while pending:
            inspected += 1
            if inspected > 100000:
                raise ValueError("source policy inspection exceeds limit")
            item = pending.pop()
            if isinstance(item, dict):
                if len(item) + len(pending) + inspected > 100000:
                    raise ValueError("source policy inspection exceeds limit")
                for key, value in item.items():
                    if isinstance(key, str) and re.sub(r"[^a-z0-9]", "", key.lower()) == "sourcekey" and isinstance(value, str):
                        declared = value.strip().lower().replace("_", "-")
                        if (declared in ("jll", "jll-main", "jll-investor", "avison-young")
                                and declared != plan_value["source_key"]):
                            raise ValueError("conflicting protected source declaration")
                pending.extend(item.keys())
                pending.extend(item.values())
            elif isinstance(item, list):
                if len(item) + len(pending) + inspected > 100000:
                    raise ValueError("source policy inspection exceeds limit")
                pending.extend(item)
            elif isinstance(item, str):
                for match in URL_TOKEN.finditer(item):
                    url = match.group().rstrip(".,;:!?)]}")
                    url = re.sub(r"^https?://", lambda m: m.group().lower(), url, flags=re.IGNORECASE)
                    http_url(url)
                    host = (urlsplit(url).hostname or "").lower().rstrip(".")
                    if host in ("jll.com", "jllproperty.com") or host.endswith((".jll.com", ".jllproperty.com")):
                        raise ValueError("JLL URL requires its source-specific withheld-content adapter")
