"""Partition settled CBRE page evidence. This module does not authorize writes."""

import hashlib
import json
import re
from collections import defaultdict


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _key(document):
    if not isinstance(document, dict):
        return None
    value = document.get("Common.PrimaryKey")
    return value if isinstance(value, str) and value and not re.search(r"\s", value) else None


def _producer_key(document):
    if not isinstance(document, dict):
        return None
    value = document.get("Common.PrimaryKey")
    return " ".join(value.split()) or None if isinstance(value, str) else None


def _flatten_one(documents):
    return [item for part in documents for item in (part if isinstance(part, list) else [part])]


def project(payload, *, generation, attempt, custody, expected_page_size):
    """Partition exact settled JSONL bytes without asserting current inventory.

    Returned observations retain parsed source documents and request-mode labels;
    neither those labels nor this partition establish tenure or target identity.
    The caller must revalidate custody and target identity at any write boundary.
    """
    if (not isinstance(payload, bytes) or not isinstance(generation, str) or not generation
            or type(attempt) is not int or attempt < 1 or not isinstance(custody, dict)
            or custody.get("state") not in {"sealed_local", "sealed_local_partial"}
            or custody.get("coverage") != "unverified" or custody.get("outcome") != "source_error"
            or type(custody.get("bytes")) is not int or custody["bytes"] != len(payload)
            or hashlib.sha256(payload).hexdigest() != custody.get("sha256")
            or not payload.endswith(b"\n")):
        raise ValueError("CBRE sidecar custody is invalid")
    try:
        lines = payload.splitlines()
        values = [json.loads(line, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                  for line in lines]
    except (ValueError, UnicodeError) as exc:
        raise ValueError("CBRE sidecar JSON is invalid") from exc
    header = {"schema": "cbre-partial-pages/v1", "sourceKey": "cbre",
              "generation": generation, "attempt": attempt}
    if (not values or values[0] != header or type(custody.get("observationRecords")) is not int
            or custody["observationRecords"] != len(values) - 1):
        raise ValueError("CBRE sidecar generation or count differs")
    by_key = defaultdict(list)
    held = []
    tainted = set()
    for record_index, record in enumerate(values[1:], 1):
        if (not isinstance(record, dict) or record.get("transaction") not in {"sale", "lease"}
                or type(record.get("page")) is not int or record["page"] < 1
                or type(record.get("pass")) is not int or record["pass"] < 1
                or not isinstance(record.get("observedAt"), str) or not record["observedAt"]
                or not isinstance(record.get("documents"), list)):
            raise ValueError("CBRE observation metadata invalid")
        kind = record.get("kind")
        if kind == "validated_page":
            documents = record["documents"]
        elif kind == "cbre_rejected_page":
            if (record.get("qualification") != "unverified_page"
                    or record.get("rejectedDocumentsIndexBasis") != "documents_flat_1"
                    or record.get("rejection") not in {"document_validation", "page_envelope"}):
                raise ValueError("CBRE rejected page metadata invalid")
            if record["rejection"] == "page_envelope":
                for response in record["documents"]:
                    if isinstance(response, dict) and isinstance(response.get("Documents"), list):
                        for document in _flatten_one(response["Documents"]):
                            key = _producer_key(document)
                            if key is not None:
                                tainted.add(key)
                held.append({"reason": "page_envelope", "record_index": record_index, "record": record})
                continue
            response = record["documents"]
            if len(response) != 1 or not isinstance(response[0], dict) or not isinstance(response[0].get("Documents"), list):
                raise ValueError("CBRE rejected page response invalid")
            documents = _flatten_one(response[0]["Documents"])
            page_size = record.get("pageSize")
            total = response[0].get("DocumentCount")
            missing_page = object()
            provider_page = next((response[0][field] for field in ("Page", "PageNumber", "CurrentPage")
                                  if field in response[0]), missing_page)
            envelope_ok = (type(expected_page_size) is int and expected_page_size > 0
                           and type(page_size) is int and page_size == expected_page_size
                           and type(total) is int and total >= 0
                           and (provider_page is missing_page or (type(provider_page) is int and provider_page == record["page"]))
                           and ("PageSize" not in response[0] or (type(response[0]["PageSize"]) is int
                               and response[0]["PageSize"] == page_size))
                           and len(documents) == max(0, min(page_size, total - (record["page"] - 1) * page_size)))
            if not envelope_ok:
                for document in documents:
                    key = _producer_key(document)
                    if key is not None:
                        tainted.add(key)
                held.append({"reason": "page_envelope_unverified", "record_index": record_index, "record": record})
                continue
            counts = defaultdict(int)
            for document in documents:
                key = _producer_key(document)
                if key is not None:
                    counts[key] += 1
            invalid = [index for index, document in enumerate(documents)
                       if _producer_key(document) is None or counts[_producer_key(document)] > 1]
            if record.get("rejectedDocuments") != invalid:
                raise ValueError("CBRE rejected document indexes differ")
        else:
            raise ValueError("unsupported CBRE sidecar observation")
        for index, document in enumerate(documents):
            observation = {"record_index": record_index, "document_index": index,
                           "request_transaction": record["transaction"], "pass": record["pass"],
                           "page": record["page"], "observed_at": record["observedAt"],
                           "document": document}
            key = _key(document)
            if key is None:
                normalized = _producer_key(document)
                if normalized is not None:
                    tainted.add(normalized)
                held.append({"reason": "missing_identity", **observation})
            elif kind == "cbre_rejected_page" and index in record["rejectedDocuments"]:
                tainted.add(key)
                held.append({"reason": "duplicate_identity", "source_id": key, **observation})
            else:
                by_key[key].append(observation)
    candidates = []
    for key, observations in sorted(by_key.items()):
        # Different representations of the same source ID can reflect changing
        # inventory or a conflicting identity; retain all, do not select one.
        variants = {json.dumps(item["document"], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    for item in observations}
        pages = defaultdict(list)
        for item in observations:
            pages[(item["request_transaction"], item["pass"])].append(item["page"])
        if key in tainted or len(variants) != 1 or any(len(page_list) > 1 for page_list in pages.values()):
            held.append({"reason": "conflicting_identity", "source_id": key, "observations": observations})
        else:
            candidates.append({"source_id": key, "observations": observations})
    return {"source_key": "cbre", "generation": generation, "attempt": attempt,
            "sidecar_sha256": custody["sha256"], "whole_source_coverage": False,
            "write_authorized": False, "candidates": candidates, "held": held}
