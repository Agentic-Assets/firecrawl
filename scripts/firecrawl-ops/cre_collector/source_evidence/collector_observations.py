"""Split and restore unqualified collector observations without interpreting them."""
import copy

MAX_RECORDS = 10000


def split(artifact):
    """Return context, ordered group inventory and held values without mutating input."""
    context = copy.deepcopy({key: value for key, value in artifact.items() if key != "listings"})
    groups = []
    observations = []
    for source_index, source in enumerate(context["sources"]):
        values = source.get("retainedObservations")
        if not isinstance(values, list) or not values:
            continue
        if len(artifact["listings"]) + len(observations) + len(values) > MAX_RECORDS:
            raise ValueError("combined collector record count exceeds limit")
        groups.append({"sourceIndex": source_index, "count": len(values)})
        observations.extend({"sourceIndex": source_index, "observationIndex": index, "value": value}
                            for index, value in enumerate(values))
        source["retainedObservations"] = []
    return context, groups, observations


def restore(context, records, groups, observations):
    """Require a canonical complete inventory, then restore original carrier arrays."""
    if (not isinstance(context, dict) or "listings" in context
            or not isinstance(context.get("sources"), list) or not isinstance(records, list)
            or not isinstance(groups, list) or not groups or not isinstance(observations, list)
            or len(records) + len(observations) > MAX_RECORDS):
        raise ValueError("invalid split collector observation shape")
    restored = copy.deepcopy(context)
    sources = restored["sources"]
    position = 0
    previous_source = -1
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"sourceIndex", "count"}:
            raise ValueError("invalid collector observation group")
        source_index, count = group["sourceIndex"], group["count"]
        if (type(source_index) is not int or source_index <= previous_source
                or source_index >= len(sources) or type(count) is not int or count < 1
                or count > MAX_RECORDS - position or position + count > len(observations)):
            raise ValueError("invalid collector observation group bounds")
        source = sources[source_index]
        if (not isinstance(source, dict) or not isinstance(source.get("retainedObservations"), list)
                or source["retainedObservations"] != []):
            raise ValueError("collector observation placeholder mismatch")
        values = []
        for index in range(count):
            entry = observations[position]
            if (not isinstance(entry, dict) or set(entry) != {"sourceIndex", "observationIndex", "value"}
                    or type(entry["sourceIndex"]) is not int or entry["sourceIndex"] != source_index
                    or type(entry["observationIndex"]) is not int or entry["observationIndex"] != index):
                raise ValueError("collector observation order or pointer mismatch")
            values.append(copy.deepcopy(entry["value"]))
            position += 1
        source["retainedObservations"] = values
        previous_source = source_index
    if position != len(observations):
        raise ValueError("collector observation inventory is incomplete")
    restored["listings"] = copy.deepcopy(records)
    return restored
