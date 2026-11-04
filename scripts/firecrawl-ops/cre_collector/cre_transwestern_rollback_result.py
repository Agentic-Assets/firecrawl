"""Attempt-bound receipt for the Transwestern protected-image guard."""

import json

from cre_image_transition_log import read_outcome, valid_binding
from cre_transwestern_image_outcome import expected_binding

PREFIX = "CRE_TRANSWESTERN_GUARD_RESULT "
SCHEMA = "transwestern-image-guard-result-v1"
OUTCOME = "protected_image_multiplicity_refused"


def result_sql(binding):
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid Transwestern guard binding")
    payload = json.dumps({"schema": SCHEMA, "source_key": "transwestern",
                          "outcome": OUTCOME, "binding": binding},
                         sort_keys=True, separators=(",", ":"))
    literal = (PREFIX + payload).replace("'", "''")
    return "\\pset format unaligned\n\\pset tuples_only on\nSELECT '" + literal + "';\n"


def read_result(path, binding):
    if not valid_binding(binding, require_collector_git_sha=True):
        raise ValueError("invalid Transwestern guard binding")
    evidence = read_outcome(path, PREFIX)
    result = evidence["outcome"]
    if (not isinstance(result, dict)
            or set(result) != {"schema", "source_key", "outcome", "binding"}
            or result["schema"] != SCHEMA or result["source_key"] != "transwestern"
            or result["outcome"] != OUTCOME
            or not valid_binding(result["binding"], require_collector_git_sha=True)
            or result["binding"] != binding):
        raise ValueError("invalid Transwestern guard result")
    return evidence
