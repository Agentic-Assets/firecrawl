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
JLL_MEMBER_COUNT = 16
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
        "no_write": admission.NO_WRITE,
    }


def _collection_intent_sha256(members: list[dict[str, str]]) -> str:
    return sha256(
        {
            "sourceKey": "jll",
            "enumerationBodySha256": JLL_ENUMERATION_BODY_SHA256,
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
        or not provider_id.isdigit()
        or not isinstance(url, str)
        or not url.startswith("https://property.jll.com/listings/")
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


def build_jll_bundle(
    *, receipt_root: Path, receipt_manifest: Path, admission_root: Path
) -> dict[str, str]:
    """Revalidate a sealed JLL receipt manifest and write one review bundle."""
    with _open_private_root(receipt_root, "receipt root") as root:
        manifest, raw = _read_private_json(
            root, receipt_manifest, MAX_MANIFEST_BYTES, "JLL receipt manifest"
        )
        verified = _validate_manifest(manifest, raw, root)
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
