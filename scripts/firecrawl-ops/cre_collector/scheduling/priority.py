"""Fair ordering for a fresh scheduled generation after a settled deadline."""

from collections.abc import Mapping, Sequence


def after_deadline(sources: Sequence[str], parent: Mapping, child: Mapping) -> list[str]:
    """Put unstarted and interrupted work first, retaining every configured source.

    Verified outcomes inform order only; the new generation must still run its
    own source gates and readbacks. Local failures remain eligible every cycle.
    """
    selected = list(sources)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("source scope must be nonempty and unique")
    records = parent["sources"]
    child_records = child["sources"]
    if set(records) != set(selected) or set(child_records) != set(selected):
        raise ValueError("source scope differs")

    def rank(source):
        state = records[source]["state"]
        attempts = child_records[source].get("attempts")
        if not isinstance(attempts, list):
            raise TypeError("source attempts missing")
        if state == "interrupted":
            return 0 if not attempts else 1
        if state == "failed_source":
            return 2
        if state == "qualified_partial_subset":
            return 3
        if state == "complete":
            return 4
        raise ValueError("source outcome is not terminal")

    return sorted(selected, key=rank)
