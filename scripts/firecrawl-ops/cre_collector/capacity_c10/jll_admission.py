"""Offline JLL-only C10 receipt-to-authority admission contract.

This is deliberately not a collector.  The TypeScript source-owned JLL
producer creates the bounded public receipt set through a controller-issued
one-shot transport.  This module only verifies its immutable manifest and
renders a reviewable, separate authority proposal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import admission
from .admission_chain import (
    MAX_BUNDLE_BYTES,
    MAX_MANIFEST_BYTES,
    _open_private_root,
    _read_private_json,
    _write_private_json,
)
from .authority import (
    JLL_AUTHORITY_KIND,
    load_jll_authority,
    repository_implementation_sha256,
)
from .contracts import (
    ARM_SEQUENCE,
    C10Error,
    require_no_write,
    require_sha256,
    sha256,
)
from .policy import load_policy

JLL_COHORT_KIND = "cre_capacity_c10_jll_v1_cohort"
JLL_BUNDLE_KIND = "cre_capacity_c10_jll_v1_admission_bundle"
JLL_PLAN_KIND = "cre_capacity_c10_jll_v1_plan"
JLL_RECEIPT_MANIFEST_KIND = "cre_capacity_c10_jll_v1_receipt_manifest"
JLL_CONTROLLER_COMPLETION_KIND = "cre_capacity_c10_jll_v1_controller_completion"
JLL_CONTROLLER_COMPLETION_STEM = "jll-admission-completion"
_CONTROLLER_COMPLETION_NAME = re.compile(
    rf"{JLL_CONTROLLER_COMPLETION_STEM}-([0-9a-f]{{64}})\.sealed"
)
_MAX_CONTROLLER_COMPLETION_BYTES = 64 * 1024
# Written by production when sidecar teardown could not be proven.
JLL_ADMISSION_QUARANTINE_NAME = "jll-admission-quarantine.json"
JLL_MEMBER_COUNT = 16
JLL_SELECTION_RULE = "jll-canonical-url-lexicographic-v1"
JLL_ENUMERATION_BODY_SHA256 = (
    "2f04bb146d4dcf85efb95a6e4d88f319029690fec804b7cc5379ff4d5930ad38"
)
_RECEIPT_NO_WRITE = {
    "database_writes": 0,
    "cache_writes": 0,
    "status_writes": 0,
    "scheduler_writes": 0,
    "model_or_ocr_changes": 0,
}


def _jll_intent() -> dict[str, Any]:
    """The one source-owned collection graph; callers cannot parameterize it."""
    return {
        "source_key": "jll",
        "enumeration": {"transaction": "sale", "property_type": "office", "page": 1},
        "member_count": JLL_MEMBER_COUNT,
        "selection_rule": JLL_SELECTION_RULE,
        "no_write": admission.NO_WRITE,
    }


JLL_GRAPHQL_URL = "https://property.jll.com/api/graphql"
JLL_ENUMERATION_CARD_ID = "jll-enumeration-0"
# ``jll-canonical-url-lexicographic-v1``.  The TypeScript source selector in
# receipts/strict_detail/jll_admission.ts implements the identical grammar;
# tests/fixtures/c10_jll_selection_vectors.json pins both implementations.
_JLL_ADMISSION_ROUTE = re.compile(
    r"(?:https://property\.jll\.com)?/listings/([A-Za-z0-9][A-Za-z0-9._~-]*)/?(?:[?#][^\n\r\u2028\u2029]*)?"
)
_JLL_PROVIDER_ID = re.compile(r"[0-9]+")


def select_jll_admission_members(payload: Any) -> dict[str, Any]:
    """Recompute the source-owned JLL cohort from one native GraphQL payload.

    Any malformed or duplicate candidate, or fewer than sixteen candidates,
    rejects the whole enumeration.  Canonical routes are ASCII, so code-point
    ordering here equals the TypeScript UTF-16 code-unit ordering.
    """
    if not isinstance(payload, Mapping):
        raise C10Error("JLL admission enumeration envelope is invalid")
    errors = payload.get("errors", [])
    data = payload.get("data")
    properties = data.get("properties") if isinstance(data, Mapping) else None
    count = properties.get("count") if isinstance(properties, Mapping) else None
    items = properties.get("items") if isinstance(properties, Mapping) else None
    if (
        not isinstance(errors, list)
        or errors
        or isinstance(count, bool)
        or not isinstance(count, int | float)
        or not float(count).is_integer()
        or count < 0
        or not isinstance(items, list)
        or not all(isinstance(item, Mapping) for item in items)
    ):
        raise C10Error("JLL admission enumeration envelope is invalid")
    ids: set[str] = set()
    routes: set[str] = set()
    candidates: list[tuple[str, str]] = []
    for item in items:
        provider_id, page_url = item.get("id"), item.get("pageUrl")
        match = (
            _JLL_ADMISSION_ROUTE.fullmatch(page_url)
            if isinstance(page_url, str)
            else None
        )
        if (
            not isinstance(provider_id, str)
            or _JLL_PROVIDER_ID.fullmatch(provider_id) is None
            or match is None
        ):
            raise C10Error(
                "JLL admission enumeration contains a noncanonical candidate"
            )
        route = f"https://property.jll.com/listings/{match.group(1)}"
        if provider_id in ids or route in routes:
            raise C10Error("JLL admission enumeration contains duplicate candidates")
        ids.add(provider_id)
        routes.add(route)
        candidates.append((route, provider_id))
    if len(candidates) < JLL_MEMBER_COUNT:
        raise C10Error(
            "JLL admission enumeration has insufficient canonical candidates"
        )
    candidates.sort(key=lambda candidate: candidate[0])
    members = [
        {"key": f"jll-{index + 1}", "provider_id": provider_id, "canonical_url": route}
        for index, (route, provider_id) in enumerate(candidates[:JLL_MEMBER_COUNT])
    ]
    return {
        "rule": JLL_SELECTION_RULE,
        "candidate_count": len(candidates),
        "members": members,
        "digest": _selection_digest(members),
    }


def _parse_enumeration_body(raw: bytes) -> Any:
    """Decode exactly like the TypeScript fatal UTF-8 decoder plus JSON.parse."""

    def _reject_constant(_: str) -> Any:
        raise ValueError("non-finite JSON constant")

    try:
        return json.loads(raw.decode("utf-8-sig"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        raise C10Error("JLL admission enumeration envelope is invalid") from exc


def _selection_digest(members: list[dict[str, str]]) -> str:
    return sha256(
        {
            "rule": JLL_SELECTION_RULE,
            "memberRoutes": [member["canonical_url"] for member in members],
            "providerIds": [member["provider_id"] for member in members],
        }
    )


def _collection_intent_sha256(members: list[dict[str, str]]) -> str:
    return sha256(
        {
            "sourceKey": "jll",
            "enumerationBodySha256": JLL_ENUMERATION_BODY_SHA256,
            "selectionRule": JLL_SELECTION_RULE,
            "selectionDigest": _selection_digest(members),
            "memberRoutes": [member["canonical_url"] for member in members],
        }
    )


def _validate_member(value: Any, index: int) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "key",
        "provider_id",
        "canonical_url",
    }:
        raise C10Error("JLL admission member schema is invalid")
    expected_key = f"jll-{index + 1}"
    provider_id, url = value.get("provider_id"), value.get("canonical_url")
    if (
        value.get("key") != expected_key
        or not isinstance(provider_id, str)
        or _JLL_PROVIDER_ID.fullmatch(provider_id) is None
        or not isinstance(url, str)
        or _JLL_ADMISSION_ROUTE.fullmatch(url) is None
        or not url.startswith("https://property.jll.com/listings/")
        or url.endswith("/")
        or "?" in url
        or "#" in url
    ):
        raise C10Error("JLL admission member identity is invalid")
    return {"key": expected_key, "provider_id": provider_id, "canonical_url": url}


def _validate_public_receipt(value: Any, *, stage: str, member_key: str | None) -> str:
    if not isinstance(value, Mapping):
        raise C10Error("JLL public receipt is invalid")
    required = {
        "schemaVersion",
        "kind",
        "stage",
        "sourceKey",
        "memberKey",
        "binding",
        "noWrite",
        "requestAccounting",
        "privateArtifactSha256",
        "receiptSha256",
    }
    if (
        set(value) != required
        or value.get("schemaVersion") != 1
        or value.get("kind") != "cre_capacity_c10_private_source_receipt"
        or value.get("stage") != stage
        or value.get("sourceKey") != "jll"
        or value.get("memberKey") != member_key
        or value.get("noWrite") != _RECEIPT_NO_WRITE
    ):
        raise C10Error("JLL public receipt does not match the fixed source contract")
    binding = value.get("binding")
    accounting = value.get("requestAccounting")
    if (
        not isinstance(binding, Mapping)
        or set(binding)
        != {
            "planSha256",
            "cohortSha256",
            "policySha256",
            "sourceSha256",
            "armSha256",
            "implementationSha256",
        }
        or not isinstance(accounting, Mapping)
        or set(accounting) != {"logicalRequests", "attempts", "retries", "eventsSha256"}
        or accounting.get("retries") != 0
        or type(accounting.get("logicalRequests")) is not int
        or type(accounting.get("attempts")) is not int
        or accounting["logicalRequests"] < 1
        or accounting["attempts"] != accounting["logicalRequests"]
    ):
        raise C10Error("JLL public receipt binding or accounting is invalid")
    for label, digest in [
        *binding.items(),
        ("private artifact", value.get("privateArtifactSha256")),
    ]:
        require_sha256(digest, str(label))
    unsigned = {key: item for key, item in value.items() if key != "receiptSha256"}
    if value.get("receiptSha256") != sha256(unsigned):
        raise C10Error("JLL public receipt digest is invalid")
    return value["receiptSha256"]


def _revalidate_artifact_index(
    root: Any, artifacts: Any, private_hashes: set[str]
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Descriptor-relative rehash of every controller-recorded sealed artifact."""
    if not isinstance(artifacts, list) or not artifacts:
        raise C10Error("JLL receipt manifest artifact index is missing")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    indexed_hashes: set[str] = set()
    contents: dict[str, bytes] = {}
    for item in artifacts:
        if not isinstance(item, Mapping) or set(item) != {"name", "sha256", "bytes"}:
            raise C10Error("JLL receipt artifact index entry is invalid")
        name, digest, size = item.get("name"), item.get("sha256"), item.get("bytes")
        if (
            not isinstance(name, str)
            or name in {"", ".", ".."}
            or "/" in name
            or name in names
            or type(size) is not int
            or size < 1
        ):
            raise C10Error("JLL receipt artifact index name is invalid")
        require_sha256(digest, "JLL receipt artifact")
        root.recheck()
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root.fd)
        except OSError as exc:
            raise C10Error("JLL sealed receipt artifact is unavailable") from exc
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != size
            ):
                raise C10Error("JLL sealed receipt artifact metadata is invalid")
            hasher = hashlib.sha256()
            remaining = size
            raw = b""
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    raise C10Error("JLL sealed receipt artifact was truncated")
                hasher.update(chunk)
                raw += chunk
                remaining -= len(chunk)
            if os.read(fd, 1) or hasher.hexdigest() != digest:
                raise C10Error("JLL sealed receipt artifact digest is invalid")
        finally:
            os.close(fd)
        names.add(name)
        indexed_hashes.add(digest)
        contents[digest] = raw
        normalized.append({"name": name, "sha256": digest, "bytes": size})
    root.recheck()
    if not private_hashes <= indexed_hashes:
        raise C10Error("JLL public receipt refers to an unindexed sealed artifact")
    return normalized, contents


def _bind_stage_artifact(
    receipt: Mapping[str, Any], raw: bytes, *, stage: str, member_key: str | None
) -> None:
    """Bind a public receipt to its unique, sealed private stage payload."""
    try:
        private = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise C10Error("JLL sealed stage artifact JSON is invalid") from exc
    if (
        not isinstance(private, Mapping)
        or set(private)
        != {
            "binding",
            "sourceKey",
            "stage",
            "memberKey",
            "requestAccounting",
            "evidence",
        }
        or private.get("binding") != receipt.get("binding")
        or private.get("sourceKey") != "jll"
        or private.get("stage") != stage
        or private.get("memberKey") != member_key
        or private.get("requestAccounting") != receipt.get("requestAccounting")
    ):
        raise C10Error("JLL sealed stage artifact does not bind its public receipt")


def _json_artifact(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None


def _verify_source_selection(
    manifest: Mapping[str, Any],
    members: list[dict[str, str]],
    artifact_contents: Mapping[str, bytes],
) -> None:
    """Recompute the cohort from the sealed native enumeration response body.

    The manifest's member list, selection digest, and the TypeScript selection
    recorded in the enumeration stage artifact are all untrusted claims until
    this independent Python recomputation matches them.  The enumeration event
    is bound to the public receipt through its request-accounting digest, and
    its response body is bound to the event by content address.
    """
    receipt = manifest["enumeration"]
    stage = json.loads(artifact_contents[receipt["privateArtifactSha256"]])
    evidence = stage.get("evidence")
    claimed = evidence.get("selection") if isinstance(evidence, Mapping) else None
    if not isinstance(claimed, Mapping) or set(evidence) != {
        "selection",
        "memberGraph",
    }:
        raise C10Error("JLL enumeration stage artifact lacks its source selection")
    events = [
        (digest, value)
        for digest, raw in artifact_contents.items()
        if isinstance(value := _json_artifact(raw), Mapping)
        and set(value) == {"binding", "card", "response"}
        and isinstance(value.get("card"), Mapping)
        and value["card"].get("id") == JLL_ENUMERATION_CARD_ID
    ]
    if len(events) != 1:
        raise C10Error(
            "JLL receipt manifest needs exactly one sealed enumeration event"
        )
    event_sha256, event = events[0]
    card, response = event["card"], event["response"]
    body_sha256 = response.get("bodySha256") if isinstance(response, Mapping) else None
    if (
        event.get("binding") != receipt.get("binding")
        or card.get("sourceKey") != "jll"
        or card.get("stage") != "enumeration"
        or card.get("method") != "POST"
        or card.get("url") != JLL_GRAPHQL_URL
        or card.get("bodySha256") != JLL_ENUMERATION_BODY_SHA256
        or not isinstance(response, Mapping)
        or type(response.get("status")) is not int
        or not 200 <= response["status"] <= 299
        or response.get("finalUrl") != JLL_GRAPHQL_URL
        or response.get("challengeDetected") is not False
        or response.get("redirectCount") != 0
        or response.get("providerAttempts") != 1
        or response.get("cacheMode") != "no-store"
        or not isinstance(body_sha256, str)
        or response.get("bodyArtifactSha256") != body_sha256
        or body_sha256 not in artifact_contents
    ):
        raise C10Error("JLL sealed enumeration event does not bind its response")
    body = artifact_contents[body_sha256]
    accounting_event = {
        "cardId": JLL_ENUMERATION_CARD_ID,
        "outcome": "accepted",
        "status": response["status"],
        "elapsedMs": response.get("elapsedMs"),
        "bytes": len(body),
        "bodySha256": body_sha256,
        "privateEventSha256": event_sha256,
    }
    accounting = receipt["requestAccounting"]
    if accounting.get("logicalRequests") != 1 or accounting.get(
        "eventsSha256"
    ) != sha256([accounting_event]):
        raise C10Error("JLL enumeration receipt does not account for its sealed event")
    selection = select_jll_admission_members(_parse_enumeration_body(body))
    expected_claim = {
        "rule": JLL_SELECTION_RULE,
        "candidateCount": selection["candidate_count"],
        "selectedMembers": [
            {
                "key": member["key"],
                "providerId": member["provider_id"],
                "canonicalUrl": member["canonical_url"],
            }
            for member in selection["members"]
        ],
        "digest": selection["digest"],
    }
    if (
        selection["members"] != members
        or manifest.get("selection_digest") != selection["digest"]
        or dict(claimed) != expected_claim
    ):
        raise C10Error(
            "JLL receipt manifest members do not match the recomputed source selection"
        )


def _verify_member_projection(raw: bytes, member: Mapping[str, str]) -> None:
    stage = json.loads(raw)
    evidence = stage.get("evidence")
    projected = evidence.get("member") if isinstance(evidence, Mapping) else None
    if (
        not isinstance(projected, Mapping)
        or projected.get("canonicalUrl") != member["canonical_url"]
        or projected.get("providerId") != member["provider_id"]
    ):
        raise C10Error("JLL member stage artifact does not bind its selected member")


def _validate_manifest(
    manifest: Mapping[str, Any], raw: bytes, root: Path
) -> dict[str, Any]:
    required = {
        "schema_version",
        "kind",
        "receipt_root",
        "collection_intent",
        "members",
        "enumeration",
        "member_receipts",
        "artifacts",
        "adapter_implementation_sha256",
        "no_write",
        "collection_intent_sha256",
        "selection_digest",
        "manifest_sha256",
    }
    if (
        set(manifest) != required
        or manifest.get("schema_version") != 1
        or manifest.get("kind") != JLL_RECEIPT_MANIFEST_KIND
        or manifest.get("receipt_root") != str(root.path)
        or manifest.get("collection_intent") != _jll_intent()
        or manifest.get("no_write") != admission.NO_WRITE
    ):
        raise C10Error("JLL receipt manifest schema is invalid")
    unsigned = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    if manifest.get("manifest_sha256") != sha256(unsigned):
        raise C10Error("JLL receipt manifest digest is invalid")
    members = manifest.get("members")
    receipts = manifest.get("member_receipts")
    if (
        not isinstance(members, list)
        or not isinstance(receipts, list)
        or len(members) != JLL_MEMBER_COUNT
        or len(receipts) != JLL_MEMBER_COUNT
    ):
        raise C10Error(
            "JLL receipt manifest requires exactly sixteen members and receipts"
        )
    normalized = [
        _validate_member(member, index) for index, member in enumerate(members)
    ]
    if (
        len({member["provider_id"] for member in normalized}) != JLL_MEMBER_COUNT
        or len({member["canonical_url"] for member in normalized}) != JLL_MEMBER_COUNT
    ):
        raise C10Error("JLL receipt manifest member identities are not unique")
    if manifest.get("collection_intent_sha256") != _collection_intent_sha256(
        normalized
    ):
        raise C10Error(
            "JLL receipt manifest collection intent is not source-card-bound"
        )
    if manifest.get("selection_digest") != _selection_digest(normalized):
        raise C10Error("JLL receipt manifest selection digest is invalid")
    enum_digest = _validate_public_receipt(
        manifest.get("enumeration"), stage="enumeration", member_key=None
    )
    member_digests = [
        _validate_public_receipt(receipt, stage="member", member_key=member["key"])
        for member, receipt in zip(normalized, receipts, strict=True)
    ]
    artifacts, artifact_contents = _revalidate_artifact_index(
        root,
        manifest.get("artifacts"),
        {
            manifest["enumeration"]["privateArtifactSha256"],
            *(receipt["privateArtifactSha256"] for receipt in receipts),
        },
    )
    stage_receipts = [manifest["enumeration"], *receipts]
    private_artifacts = [receipt["privateArtifactSha256"] for receipt in stage_receipts]
    if len(private_artifacts) != len(set(private_artifacts)):
        raise C10Error("JLL public receipts cannot share a sealed stage artifact")
    _bind_stage_artifact(
        manifest["enumeration"],
        artifact_contents[manifest["enumeration"]["privateArtifactSha256"]],
        stage="enumeration",
        member_key=None,
    )
    for member, receipt in zip(normalized, receipts, strict=True):
        _bind_stage_artifact(
            receipt,
            artifact_contents[receipt["privateArtifactSha256"]],
            stage="member",
            member_key=member["key"],
        )
        _verify_member_projection(
            artifact_contents[receipt["privateArtifactSha256"]], member
        )
    _verify_source_selection(manifest, normalized, artifact_contents)
    adapter = manifest.get("adapter_implementation_sha256")
    require_sha256(adapter, "JLL adapter implementation")
    if adapter != repository_implementation_sha256("jll"):
        raise C10Error("JLL receipt manifest adapter bytes are stale")
    # Every source receipt must arise from the same sealed collection binding.
    bindings = [
        manifest["enumeration"]["binding"],
        *(receipt["binding"] for receipt in receipts),
    ]
    if any(binding != bindings[0] for binding in bindings[1:]):
        raise C10Error("JLL receipt manifest contains mixed collection bindings")
    return {
        "members": normalized,
        "enumeration_receipt_sha256": enum_digest,
        "member_receipt_sha256": member_digests,
        "receipt_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "adapter_implementation_sha256": adapter,
        "collection_binding": dict(bindings[0]),
        "collection_intent_sha256": manifest["collection_intent_sha256"],
        "artifact_index_sha256": sha256(artifacts),
    }


def _verify_controller_completion(
    root: Any, manifest_name: str, raw: bytes, manifest: Mapping[str, Any]
) -> None:
    """Require the controller's post-verification completion attestation.

    A child-sealed manifest can exist in a root the controller later rejected
    (deadline, verification, or teardown failure).  Only the controller seals
    this record, and only after verifying the manifest against its own state.
    """
    root.recheck()
    try:
        entries = os.listdir(root.fd)
    except OSError as exc:
        raise C10Error("JLL controller completion is unavailable") from exc
    if any(name.startswith("jll-admission-quarantine") for name in entries):
        raise C10Error("JLL receipt root is quarantined and cannot be admitted")
    names = [
        name for name in entries if name.startswith(JLL_CONTROLLER_COMPLETION_STEM)
    ]
    if len(names) != 1:
        raise C10Error(
            "JLL receipt root lacks exactly one controller completion attestation"
        )
    match = _CONTROLLER_COMPLETION_NAME.fullmatch(names[0])
    if match is None:
        raise C10Error("JLL controller completion name is invalid")
    record, record_raw = _read_private_json(
        root,
        root.path / names[0],
        _MAX_CONTROLLER_COMPLETION_BYTES,
        "JLL controller completion",
    )
    if hashlib.sha256(record_raw).hexdigest() != match.group(1):
        raise C10Error("JLL controller completion digest is invalid")
    required = {
        "schema_version",
        "kind",
        "receipt_root",
        "receipt_manifest",
        "manifest_sha256",
        "selection_digest",
        "adapter_implementation_sha256",
        "session_sha256",
        "run_sha256",
        "completion_sha256",
    }
    unsigned = {
        key: value for key, value in record.items() if key != "completion_sha256"
    }
    if (
        set(record) != required
        or record.get("schema_version") != 1
        or record.get("kind") != JLL_CONTROLLER_COMPLETION_KIND
        or record.get("completion_sha256") != sha256(unsigned)
        or record.get("receipt_root") != str(root.path)
        or record.get("receipt_manifest")
        != {
            "name": manifest_name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
        }
        or record.get("manifest_sha256") != manifest.get("manifest_sha256")
        or record.get("selection_digest") != manifest.get("selection_digest")
        or record.get("adapter_implementation_sha256")
        != manifest.get("adapter_implementation_sha256")
    ):
        raise C10Error(
            "JLL controller completion does not attest this receipt manifest"
        )
    require_sha256(record.get("session_sha256"), "JLL controller session")
    require_sha256(record.get("run_sha256"), "JLL controller run")


def build_jll_bundle(
    *, receipt_root: Path, receipt_manifest: Path, admission_root: Path
) -> dict[str, str]:
    """Revalidate a sealed JLL receipt manifest and write one review bundle."""
    with _open_private_root(receipt_root, "receipt root") as root:
        manifest, raw = _read_private_json(
            root, receipt_manifest, MAX_MANIFEST_BYTES, "JLL receipt manifest"
        )
        verified = _validate_manifest(manifest, raw, root)
        _verify_controller_completion(root, receipt_manifest.name, raw, manifest)
    cohort_unsigned = {
        "schema_version": 1,
        "kind": JLL_COHORT_KIND,
        "collection_intent_sha256": verified["collection_intent_sha256"],
        **verified,
        "no_write": admission.NO_WRITE,
    }
    cohort = {**cohort_unsigned, "cohort_sha256": sha256(cohort_unsigned)}
    bundle_unsigned = {
        "schema_version": 1,
        "kind": JLL_BUNDLE_KIND,
        "receipt_root": str(receipt_root),
        "receipt_manifest_name": receipt_manifest.name,
        "receipt_manifest_sha256": verified["receipt_manifest_sha256"],
        "cohort": cohort,
        "no_write": admission.NO_WRITE,
    }
    bundle = {**bundle_unsigned, "bundle_sha256": sha256(bundle_unsigned)}
    with _open_private_root(admission_root, "admission root") as root:
        path = _write_private_json(
            root, f"jll-admission-{cohort['cohort_sha256']}.json", bundle
        )
    return {
        "path": str(path),
        "cohort_sha256": cohort["cohort_sha256"],
        "bundle_sha256": bundle["bundle_sha256"],
    }


def _load_jll_bundle(bundle_path: Path) -> dict[str, Any]:
    with _open_private_root(bundle_path.parent, "admission root") as root:
        bundle, _ = _read_private_json(
            root, bundle_path, MAX_BUNDLE_BYTES, "JLL admission bundle"
        )
    required = {
        "schema_version",
        "kind",
        "receipt_root",
        "receipt_manifest_name",
        "receipt_manifest_sha256",
        "cohort",
        "no_write",
        "bundle_sha256",
    }
    if (
        set(bundle) != required
        or bundle.get("schema_version") != 1
        or bundle.get("kind") != JLL_BUNDLE_KIND
        or not isinstance(bundle.get("cohort"), Mapping)
    ):
        raise C10Error("JLL admission bundle schema is invalid")
    if bundle.get("bundle_sha256") != sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    ):
        raise C10Error("JLL admission bundle digest is invalid")
    require_no_write(bundle)
    return bundle


def render_jll_authority(bundle_path: Path) -> dict[str, Any]:
    """Reopen provenance and render, never install, the JLL authority proposal."""
    bundle = _load_jll_bundle(bundle_path)
    receipt_root = Path(bundle["receipt_root"])
    with _open_private_root(receipt_root, "receipt root") as root:
        manifest, raw = _read_private_json(
            root,
            root.path / bundle["receipt_manifest_name"],
            MAX_MANIFEST_BYTES,
            "JLL receipt manifest",
        )
        verified = _validate_manifest(manifest, raw, root)
        _verify_controller_completion(
            root, bundle["receipt_manifest_name"], raw, manifest
        )
    if hashlib.sha256(raw).hexdigest() != bundle["receipt_manifest_sha256"]:
        raise C10Error("JLL receipt manifest is stale or tampered")
    cohort = bundle["cohort"]
    if (
        cohort.get("cohort_sha256")
        != sha256(
            {key: value for key, value in cohort.items() if key != "cohort_sha256"}
        )
        or cohort.get("receipt_manifest_sha256") != verified["receipt_manifest_sha256"]
    ):
        raise C10Error("JLL cohort provenance no longer matches its receipts")
    policy = load_policy()
    plan = _jll_plan(cohort, policy, verified)
    return {
        "schema_version": 1,
        "kind": JLL_AUTHORITY_KIND,
        "approved_cohort_sha256": cohort["cohort_sha256"],
        "approved_plan_sha256": plan["plan_sha256"],
        "approved_receipt_manifest_sha256": verified["receipt_manifest_sha256"],
        "approved_adapter_sha256": verified["adapter_implementation_sha256"],
    }


def render_jll_plan(bundle_path: Path) -> dict[str, Any]:
    """Render the exact plan accompanying a reviewable JLL authority proposal."""
    bundle = _load_jll_bundle(bundle_path)
    receipt_root = Path(bundle["receipt_root"])
    with _open_private_root(receipt_root, "receipt root") as root:
        manifest, raw = _read_private_json(
            root,
            root.path / bundle["receipt_manifest_name"],
            MAX_MANIFEST_BYTES,
            "JLL receipt manifest",
        )
        verified = _validate_manifest(manifest, raw, root)
        _verify_controller_completion(
            root, bundle["receipt_manifest_name"], raw, manifest
        )
    if hashlib.sha256(raw).hexdigest() != bundle["receipt_manifest_sha256"]:
        raise C10Error("JLL receipt manifest is stale or tampered")
    cohort = bundle["cohort"]
    if (
        cohort.get("cohort_sha256")
        != sha256(
            {key: value for key, value in cohort.items() if key != "cohort_sha256"}
        )
        or cohort.get("receipt_manifest_sha256") != verified["receipt_manifest_sha256"]
    ):
        raise C10Error("JLL cohort provenance no longer matches its receipts")
    return _jll_plan(cohort, load_policy(), verified)


def _jll_plan(
    cohort: Mapping[str, Any], policy: Mapping[str, Any], verified: Mapping[str, Any]
) -> dict[str, Any]:
    """Build the exact, still-unapproved JLL plan from revalidated evidence."""
    profiles = admission._profiles(admission.PROFILE_CONFIG, policy["profiles"])
    plan_unsigned = {
        "schema_version": 1,
        "kind": JLL_PLAN_KIND,
        "policy_sha256": policy["policy_sha256"],
        "cohort_sha256": cohort["cohort_sha256"],
        "implementation_sha256": sha256(
            {"jll": verified["adapter_implementation_sha256"]}
        ),
        "profiles": profiles,
        "source": {
            "key": "jll",
            "cohort_member_count": JLL_MEMBER_COUNT,
            "cohort_member_sha256": sha256(cohort["members"]),
            "enumeration_receipt_sha256": cohort["enumeration_receipt_sha256"],
            "receipt_manifest_sha256": verified["receipt_manifest_sha256"],
            "collection_intent_sha256": cohort["collection_intent_sha256"],
            "collection_binding_sha256": sha256(cohort["collection_binding"]),
        },
        "no_write": admission.NO_WRITE,
        "arm_sequence": list(ARM_SEQUENCE),
    }
    return {**plan_unsigned, "plan_sha256": sha256(plan_unsigned)}


def validate_jll_plan(plan: Mapping[str, Any]) -> None:
    """Require the separately pinned JLL plan before a host can consume it."""
    required = {
        "schema_version",
        "kind",
        "policy_sha256",
        "cohort_sha256",
        "implementation_sha256",
        "profiles",
        "source",
        "no_write",
        "arm_sequence",
        "plan_sha256",
    }
    if (
        set(plan) != required
        or plan.get("schema_version") != 1
        or plan.get("kind") != JLL_PLAN_KIND
    ):
        raise C10Error("JLL C10 plan schema is invalid")
    for key in (
        "policy_sha256",
        "cohort_sha256",
        "implementation_sha256",
        "plan_sha256",
    ):
        require_sha256(plan.get(key), key)
    if (
        tuple(plan.get("arm_sequence", ())) != ARM_SEQUENCE
        or plan.get("no_write") != admission.NO_WRITE
    ):
        raise C10Error("JLL C10 plan experiment contract is invalid")
    policy = load_policy()
    if plan["policy_sha256"] != policy["policy_sha256"]:
        raise C10Error("JLL C10 plan policy is stale")
    if plan.get("profiles") != admission._profiles(
        admission.PROFILE_CONFIG, policy["profiles"]
    ):
        raise C10Error("JLL C10 plan profiles are invalid")
    source = plan.get("source")
    expected_source_fields = {
        "key",
        "cohort_member_count",
        "cohort_member_sha256",
        "enumeration_receipt_sha256",
        "receipt_manifest_sha256",
        "collection_intent_sha256",
        "collection_binding_sha256",
    }
    if (
        not isinstance(source, Mapping)
        or set(source) != expected_source_fields
        or source.get("key") != "jll"
        or source.get("cohort_member_count") != JLL_MEMBER_COUNT
    ):
        raise C10Error("JLL C10 plan source projection is invalid")
    for key in (
        "cohort_member_sha256",
        "enumeration_receipt_sha256",
        "receipt_manifest_sha256",
        "collection_intent_sha256",
        "collection_binding_sha256",
    ):
        require_sha256(source.get(key), key)
    if plan["implementation_sha256"] != sha256(
        {"jll": repository_implementation_sha256("jll")}
    ):
        raise C10Error("JLL C10 plan adapter bytes are stale")
    if plan["plan_sha256"] != sha256(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    ):
        raise C10Error("JLL C10 plan digest is invalid")
    authority = load_jll_authority()
    if (
        authority["approved_cohort_sha256"] != plan["cohort_sha256"]
        or authority["approved_plan_sha256"] != plan["plan_sha256"]
        or authority["approved_receipt_manifest_sha256"]
        != source["receipt_manifest_sha256"]
        or authority["approved_adapter_sha256"]
        != repository_implementation_sha256("jll")
    ):
        raise C10Error("JLL C10 plan lacks current separate repository authority")


def main(argv: list[str] | None = None) -> int:
    """Expose only offline validation plus an explicit non-controller refusal."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    collect = commands.add_parser("collect-jll")
    collect.add_argument("--execute", action="store_true")
    bundle = commands.add_parser("build-jll-bundle")
    bundle.add_argument("--receipt-root", type=Path, required=True)
    bundle.add_argument("--receipt-manifest", type=Path, required=True)
    bundle.add_argument("--admission-root", type=Path, required=True)
    render_plan = commands.add_parser("render-jll-plan")
    render_plan.add_argument("--bundle", type=Path, required=True)
    render_authority = commands.add_parser("render-jll-authority")
    render_authority.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "collect-jll":
            if args.execute:
                raise C10Error(
                    "JLL receipt collection requires the production controller-issued transport"
                )
            result: Any = {
                "state": "dry_run",
                "external_calls": False,
                "collection_intent": _jll_intent(),
                "requires": "controller-issued source-bound one-shot transport",
            }
        elif args.command == "build-jll-bundle":
            result = build_jll_bundle(
                receipt_root=args.receipt_root,
                receipt_manifest=args.receipt_manifest,
                admission_root=args.admission_root,
            )
        elif args.command == "render-jll-plan":
            result = render_jll_plan(args.bundle)
        else:
            result = render_jll_authority(args.bundle)
    except C10Error as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
