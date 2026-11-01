"""Create a separate, hash-bound view of fenced JSON without changing model output."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path

import runner

# One complete outer JSON fence only. Do not remove surrounding commentary,
# multiple fences, alternate languages, or content embedded within a fence.
FENCED_JSON = re.compile(r"\A```json[ \t]*\r?\n([\s\S]*?)\r?\n```[ \t]*\r?\n?\Z")


def derive(raw):
    original = json.loads(raw)
    if not isinstance(original, dict):
        raise TypeError("result must be an object")
    text = original.get("raw_output")
    normalized = None
    removed = False
    if isinstance(text, str):
        match = FENCED_JSON.fullmatch(text)
        if match:
            synthetic = {"choices": [{"message": {"content": match.group(1)},
                                      "finish_reason": original.get("finish_reason")}]}
            _, normalized, _ = runner.response_content(synthetic)
            removed = normalized is not None
        elif original.get("parsed") is not None:
            synthetic = {"choices": [{"message": {"content": text},
                                      "finish_reason": original.get("finish_reason")}]}
            _, normalized, _ = runner.response_content(synthetic)
    return {"schema": "document-input-postprocess/v1", "original_sha256": runner.digest(raw),
            "document_id": original.get("document_id"), "model": original.get("model"),
            "arm": original.get("arm"), "original_status": original.get("status"),
            "original_invalid_json": original.get("invalid_json"),
            "finish_reason": original.get("finish_reason"), "truncated": original.get("truncated"),
            "removed_outer_json_fence": removed,
            "normalized_valid": normalized is not None,
            "normalized_content": normalized}


def run(source, dest):
    # Existing source must be an owner-only directory and destinations are new.
    before = source.lstat()
    if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
        raise ValueError("source must be a private owner-only directory")
    dest.mkdir(mode=0o700, parents=False, exist_ok=False)
    files = sorted(source.glob("*.result.json"))
    counts = {"records": 0, "fence_removed": 0, "normalized_valid": 0}
    for path in files:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("result file is not private and regular")
        raw = path.read_bytes()
        result = derive(raw)
        result["original_name"] = path.name
        runner.write_new(dest / path.name, runner.canonical(result))
        counts["records"] += 1
        counts["fence_removed"] += result["removed_outer_json_fence"]
        counts["normalized_valid"] += result["normalized_valid"]
    runner.write_new(dest / "summary.json", runner.canonical({"schema": "document-input-postprocess-summary/v1", **counts}))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.source, args.out)))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(f"postprocess refused: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
