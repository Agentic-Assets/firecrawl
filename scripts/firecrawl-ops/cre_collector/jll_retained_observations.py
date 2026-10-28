"""Project a saved JLL collector artifact to private offline observations.

This is not ingestion, source authentication, or permission to disclose the
input or output. Caller retains the original; no live provider is contacted.
"""
import argparse
import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

from buildout_observation_artifact import (
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    bounded_number,
    invalid_number,
    render,
    unique_object,
    write_private,
)
from buildout_observation_io import read_consistent
from cre_ingest import (
    _jll_pricing_is_withheld,
    _jll_withholding_control,
    _safe_jll_pricing,
    _safe_jll_raw_data,
)

MAX_ROWS = 1000
# Only numeric physical facts from the existing safe projection can survive a
# hidden/unknown pass. Free text, links, contacts and arbitrary detail are not
# proven free of price disclosures or credentials.
PHYSICAL_FIELDS = ("buildingSizeSqft", "latitude", "longitude")


def unsafe_nested_shape(value):
    """Fail closed on nested passes or hidden/unknown controls anywhere."""
    if isinstance(value, list):
        return any(unsafe_nested_shape(item) for item in value)
    if not isinstance(value, dict):
        return False
    if _jll_withholding_control(value) in {"withheld", "unknown"}:
        return True
    for key, item in value.items():
        folded = key.casefold()
        normalized = re.sub(r"[^a-z0-9]", "", folded)
        if (normalized in {"hideprice", "pricewithholdingcontrol", "jllpricewithheld",
                           "searchwithholdingcontrol", "detailwithholdingcontrol", "visibility"}
                and folded != normalized):
            return True
        if normalized in {"primary", "secondarypass"}:
            return True
        if normalized == "jllpricewithheld" and item is not False:
            return True
        if (normalized in {"searchwithholdingcontrol", "detailwithholdingcontrol"}
                and (not isinstance(item, str) or item not in {"visible", "absent"})):
            return True
        if normalized == "visibility" and item != "visible":
            return True
        if normalized == "pricing":
            try:
                pricing = _safe_jll_pricing(item)
            except (TypeError, ValueError):
                return True
            if pricing is None or pricing["visibility"] != "visible":
                return True
        if unsafe_nested_shape(item):
            return True
    return False


def visibility(row):
    if unsafe_nested_shape(row):
        return "withheld_or_unknown"
    detail = row.get("jllDetail")
    pricing = detail.get("pricing") if isinstance(detail, dict) else None
    try:
        safe = _safe_jll_pricing(pricing)
        if _jll_pricing_is_withheld(row):
            return "withheld_or_unknown"
    except (TypeError, ValueError):
        return "unknown"
    if safe is None or safe["visibility"] != "visible":
        return "unknown"
    return "visible"


def project(raw):
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds byte limit")
    data = json.loads(raw, object_pairs_hook=unique_object,
                      parse_int=lambda x: bounded_number(x, floating=False),
                      parse_float=lambda x: bounded_number(x, floating=True),
                      parse_constant=invalid_number)
    if not isinstance(data, dict) or not isinstance(data.get("listings"), list):
        raise TypeError("expected collector artifact with listings")
    rows = data["listings"]
    # Existing JLL validation expects native JSON numbers, while the retained
    # evidence uses Decimal to preserve the original numeric precision.
    validation_rows = json.loads(raw, object_pairs_hook=unique_object,
                                 parse_constant=invalid_number)["listings"]
    if len(rows) > MAX_ROWS:
        raise ValueError("too many listings")
    if any(not isinstance(row, dict) or row.get("sourceKey") != "jll" for row in rows):
        raise ValueError("every listing must be a JLL object")
    states = [visibility(row) for row in validation_rows]
    # Any uncertain row may be another pass for any other row. The offline
    # adapter does not assert identities or merge; fail closed for the page.
    guarded = any(state != "visible" for state in states)
    observations = []
    for index, (row, checked, state) in enumerate(zip(rows, validation_rows, states)):
        if guarded:
            try:
                safe = _safe_jll_raw_data(checked)
            except (TypeError, ValueError):
                safe = {}
            facts = {key: row[key] for key in PHYSICAL_FIELDS
                     if key in safe and type(row[key]) in (int, Decimal)}
            omissions = ["pricing_and_arbitrary_fields_omitted_for_unresolved_visibility",
                         "parent_brokers_and_source_metadata_omitted"]
        else:
            facts = row
            omissions = ["parent_brokers_and_source_metadata_omitted"]
        observations.append({"sourcePointer": "/listings/" + str(index),
                             "identity": "unresolved", "visibility": state,
                             "facts": facts, "factsValidation": "unvalidated_source_values",
                             "omissions": omissions})
    result = {"schema": "jll-private-retained-observations-v1",
              "inputSha256": hashlib.sha256(raw).hexdigest(),
              "sourceAuthorization": "not_evaluated", "sourceCompleteness": "unverified",
              "crossRowIdentity": "unresolved", "pageWideVisibilityGuard": guarded,
              "confidentiality": "unreviewed_private_data", "observations": observations}
    encoded = (render(result) + "\n").encode("ascii")
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("output exceeds byte limit")
    return encoded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    output = project(read_consistent(args.input, MAX_INPUT_BYTES))
    write_private(args.out, output)
    print(json.dumps({"result": "private_observations_created", "sourceAuthorization": "not_evaluated"}))


if __name__ == "__main__":
    main()
