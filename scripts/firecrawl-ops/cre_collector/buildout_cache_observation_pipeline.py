"""Offline cache-audit to private artifact, integrity manifest and projection.

The input audit contains unredacted source data. No network, provider, database,
scheduler, or canonical listing write is performed. Source attribution and
caller-supplied scope remain unverified; output is private and unreviewed.
"""
import argparse
import json
import os
import stat
from pathlib import Path

import buildout_observation_artifact as artifact_tools
import buildout_observation_manifest as manifest_tools
import buildout_observation_projection as projection_tools


def pipeline(audit_bytes, *, scope):
    """Build all outputs in memory before creating any output files."""
    artifact, summary = artifact_tools.build_artifact(
        audit_bytes, source_key=scope["sourceKey"], input_kind="buildout-cache-audit"
    )
    sealed = manifest_tools.seal(audit_bytes, artifact, scope=scope, input_kind="buildout-cache-audit")
    manifest = manifest_tools.canonical(sealed) + b"\n"
    projection, projected = projection_tools.project(
        artifact, original=audit_bytes, manifest=manifest, expected_scope=scope
    )
    return artifact, manifest, projection, {"artifact": summary, "projection": projected}


def private_parent(path):
    parent = path.parent.absolute()
    metadata = os.lstat(parent)
    if (not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077
        or os.path.realpath(parent) != str(parent)):
        raise ValueError("output parent must be an owned private real directory")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source-key", required=True)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--generation-id")
    parser.add_argument("--page", type=int)
    parser.add_argument("--transaction", choices=("sale", "lease", "unknown"), default="unknown")
    args = parser.parse_args()
    scope = {"sourceKey": args.source_key, "generationId": args.generation_id,
             "captureId": args.capture_id, "page": args.page, "transaction": args.transaction}
    audit = artifact_tools.read_bounded(args.audit, artifact_tools.MAX_AUDIT_BYTES)
    artifact, manifest, projection, summary = pipeline(audit, scope=scope)
    private_parent(args.out_dir)
    # A new directory avoids overwriting any prior evidence. If a later write
    # fails, its private partial directory remains for operator inspection.
    os.mkdir(args.out_dir, 0o700)
    artifact_tools.write_private(args.out_dir / "artifact.json", artifact)
    artifact_tools.write_private(args.out_dir / "manifest.json", manifest)
    artifact_tools.write_private(args.out_dir / "projection.json", projection)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
