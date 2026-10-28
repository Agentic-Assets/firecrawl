"""Refuse unprojected Avison evidence using the source's visibility policy."""

import re

from cre_avison_price_visibility import (
    asking_amounts,
    asking_control,
    effective_control,
    project_value,
)


def _equivalent(original, projected):
    # The source projection omits withheld null-valued asking fields, while
    # normalized rows may represent that same absence explicitly as null.
    if isinstance(original, dict) and isinstance(projected, dict):
        return (all(k in original and _equivalent(original[k], v) for k, v in projected.items())
                and all(k in projected or v is None for k, v in original.items()))
    if isinstance(original, list) and isinstance(projected, list):
        return len(original) == len(projected) and all(_equivalent(a, b) for a, b in zip(original, projected))
    return type(original) is type(projected) and original == projected


def _context_controls(context):
    """Find all source-owned control witnesses without assuming a context layout."""
    pending = [context]
    controls = []
    inspected = 0
    while pending:
        item = pending.pop()
        inspected += 1
        if inspected > 100000:
            raise ValueError("Avison context inspection exceeds limit")
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            for key in item:
                normalized = re.sub(r"[^a-z0-9]", "", key.lower()) if isinstance(key, str) else ""
                if normalized in ("rawsharplaunch", "avisonpricevisibility", "latestinventoryobservation"):
                    canonical = {"rawsharplaunch": "rawSharpLaunch", "avisonpricevisibility": "avisonPriceVisibility",
                                 "latestinventoryobservation": "latestInventoryObservation"}[normalized]
                    if key != canonical:
                        raise ValueError("Avison context control key is ambiguous")
            if any(re.sub(r"[^a-z0-9]", "", str(k).lower()) == "hideprice" for k in item):
                controls.append(asking_control(item))
            if any(k in item for k in ("rawSharpLaunch", "avisonPriceVisibility", "latestInventoryObservation")):
                witness = {"sourceKey": "avison-young", **{k: item[k] for k in
                    ("rawSharpLaunch", "avisonPriceVisibility", "latestInventoryObservation") if k in item}}
                controls.append(effective_control(witness))
            pending.extend(item.values())
    return controls


def validate_records(plan):
    rows = plan["events"]
    if rows[0]["facts"][0]["kind"] == "avison_native_context":
        from source_evidence import avison_response
        header = rows[0]["facts"][0]["value"]
        observation = {**header, "records": [row["facts"][0]["value"] for row in rows[1:]]}
        avison_response.validate(observation)
        return
    kind = rows[0]["facts"][0]["kind"]
    if kind not in ("retained_capture_context", "retained_capture_context_v2"):
        raise ValueError("Avison evidence needs the collector visibility adapter")
    from source_evidence import collector
    header = rows[0]["facts"][0]["value"]
    wrapped = {**header, "records": [event["facts"][0]["value"] for event in rows[1:]
                                    if event["facts"][0]["kind"] == "source_record"]}
    if kind == "retained_capture_context_v2":
        wrapped["observations"] = [event["facts"][0]["value"] for event in rows[1:]
                                   if event["facts"][0]["kind"] == "source_observation"]
    original = collector.reconstruct(wrapped)
    records = original["listings"]
    controls = []
    for record in records:
        if not isinstance(record, dict) or record.get("sourceKey") != "avison-young":
            raise ValueError("Avison evidence has foreign provenance")
        direct_control = effective_control(record)
        raw = record.get("raw_data")
        if raw is not None:
            raw_control = effective_control(raw)
            if any(k in record for k in ("rawSharpLaunch", "avisonPriceVisibility", "latestInventoryObservation")) and direct_control != raw_control:
                raise ValueError("Avison evidence visibility controls disagree")
            controls.append(raw_control)
        else:
            controls.append(direct_control)
        if "withheld" in _context_controls(record):
            controls[-1] = "withheld"
    amounts = asking_amounts(records)
    for record, control in zip(records, controls):
        if control == "withheld" and not _equivalent(record, project_value(record, amounts)):
            raise ValueError("Avison evidence requires source-owned price projection")
    # Apply the existing context policy to the fully restored carriers too;
    # moving an observation into its own row must not remove its controls.
    context = {key: value for key, value in original.items() if key != "listings"}
    context_controls = _context_controls(context)
    if ("withheld" in controls or "withheld" in context_controls) and not _equivalent(context, project_value(context, amounts)):
        raise ValueError("Avison capture context requires source-owned price projection")
