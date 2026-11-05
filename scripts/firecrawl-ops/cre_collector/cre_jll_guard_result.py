"""Attempt-bound receipts for JLL's existing aggregate image-loss guard."""

import json

from cre_image_transition_log import artifact_binding, read_outcome, valid_binding

PREFIX = "CRE_JLL_IMAGE_GUARD_RESULT "
SCHEMA = "jll-image-guard-result-v1"


def result_sql(binding, outcome):
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid JLL guard binding")
    if outcome not in {"committed", "image_loss_refused"}:
        raise ValueError("invalid JLL guard outcome")
    payload = json.dumps({"schema": SCHEMA, "source_key": "jll", "outcome": outcome,
                          "binding": binding}, sort_keys=True, separators=(",", ":"))
    literal = (PREFIX + payload).replace("'", "''")
    return "\\pset format unaligned\n\\pset tuples_only on\nSELECT '" + literal + "';\n"


def read_result(path, binding):
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid JLL guard binding")
    evidence = read_outcome(path, PREFIX)
    result = evidence["outcome"]
    if (not isinstance(result, dict)
            or set(result) != {"schema", "source_key", "outcome", "binding"}
            or result["schema"] != SCHEMA or result["source_key"] != "jll"
            or result["outcome"] not in {"committed", "image_loss_refused"}
            or not valid_binding(result["binding"], require_collector_git_sha=True)
            or result["binding"] != binding):
        raise ValueError("invalid JLL guard receipt")
    return evidence


def expected_binding(run_dir, manifest, attempt_id):
    binding = artifact_binding(run_dir, manifest, "jll", attempt_id,
                               include_collector_git_sha=True)
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid JLL artifact binding")
    return binding
