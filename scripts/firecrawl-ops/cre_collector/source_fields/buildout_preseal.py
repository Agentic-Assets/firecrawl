"""Read-only whole-source identity hold; this never qualifies a partial write."""

import hashlib
import json
import os
import re
from pathlib import Path

from source_fields.buildout_partial import identity_preseal_sql, prepare_partition


def classify(rows, partition):
    """Validate a single source-bound identity result, including exact staged count."""
    if not isinstance(rows, list) or len(rows) != 1 or set(rows[0]) != {"brokerages", "staged", "held"}:
        raise ValueError("Buildout pre-seal result shape differs")
    row = rows[0]
    if row["brokerages"] != "1" or row["staged"] != str(len(partition["staged"])):
        raise ValueError("Buildout pre-seal source scope differs")
    held = json.loads(row["held"])
    staged = {item["external_id"] for item in partition["staged"]}
    if (not isinstance(held, list) or any(not isinstance(item, str) or not item for item in held)
            or len(held) != len(set(held)) or not set(held) <= staged):
        raise ValueError("Buildout pre-seal identities differ")
    return sorted(held)


def verify_clear(report, partition, manifest_sha):
    """Recheck a clear probe's bindings before requesting transactional refusal."""
    expected = {"schema", "scope", "manifest_sha256", "attempt_id", "staged_sha256",
                "staged_count", "held", "verdict"}
    if (not isinstance(report, dict) or set(report) != expected
            or report["schema"] != "cre-buildout-preseal-identity-v1"
            or report["scope"] != partition["scope"] or report["manifest_sha256"] != manifest_sha
            or report["attempt_id"] != partition["attempt_id"]
            or report["staged_sha256"] != partition["staged_sha256"]
            or type(report["staged_count"]) is not int or report["staged_count"] != len(partition["staged"])
            or report["held"] != [] or report["verdict"] != "clear"):
        raise ValueError("Buildout clear pre-seal binding changed")


def run_probe(artifact_path, manifest_path, manifest_sha, source, producer, target, attempt, env_file):
    """Verify bound inputs and query a repeatable-read, read-only snapshot."""
    from cre_checkpoint_refresh import git_identity
    from cre_code_handoff_git import read_file
    from cre_ingest import database_target_fingerprint_from_url, find_psql, load_db_url
    from cre_validate import PIN_STRING_LITERALS, _run_psql_script, parse_tsv

    if (not re.fullmatch(r"[a-f0-9]{64}", manifest_sha or "")
            or not re.fullmatch(r"[a-f0-9]{40}", producer or "")
            or not re.fullmatch(r"[a-f0-9]{64}", target or "")):
        raise ValueError("Buildout pre-seal binding malformed")
    manifest_raw = read_file(manifest_path)
    raw = read_file(artifact_path)
    if hashlib.sha256(manifest_raw).hexdigest() != manifest_sha or git_identity() != (producer, False):
        raise ValueError("Buildout pre-seal inputs changed")
    partition = prepare_partition(raw, json.loads(manifest_raw), source, code_sha=producer,
                                  dirty=False, target_sha=target, attempt_id=attempt)
    db_url, _ = load_db_url(env_file)
    if database_target_fingerprint_from_url(db_url).get("value") != target:
        raise ValueError("Buildout pre-seal target differs")
    sql = ("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;\n"
           + PIN_STRING_LITERALS + "\nSET LOCAL statement_timeout = '30s';\n"
           + identity_preseal_sql(partition) + "\nROLLBACK;\n")
    result = _run_psql_script(find_psql(), db_url, sql, timeout=45)
    if result.returncode != 0:
        raise RuntimeError("Buildout read-only pre-seal probe failed")
    held = classify(parse_tsv(result.stdout), partition)
    current_url, _ = load_db_url(env_file)
    if (database_target_fingerprint_from_url(current_url).get("value") != target
            or read_file(artifact_path) != raw or read_file(manifest_path) != manifest_raw
            or git_identity() != (producer, False)):
        raise ValueError("Buildout pre-seal inputs or target changed")
    return {"schema": "cre-buildout-preseal-identity-v1", "scope": partition["scope"],
            "manifest_sha256": manifest_sha, "attempt_id": attempt,
            "staged_sha256": partition["staged_sha256"], "staged_count": len(partition["staged"]),
            "held": held, "verdict": "hold" if held else "clear"}


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("artifact", "manifest", "manifest-sha", "source", "producer", "target", "attempt", "out"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--env-file")
    args = parser.parse_args(argv)
    result = run_probe(args.artifact, args.manifest, args.manifest_sha, args.source,
                       args.producer, args.target, args.attempt, args.env_file)
    fd = os.open(Path(args.out), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
