"""Unverified format assertions must stay bound and fail closed."""

from source_evidence import document_format_hint as hints


def selected(*labels):
    item = {"candidate_sha256": "a" * 64,
            "candidate": {"occurrences": [{"labels": x} for x in labels]}}
    return {"documents": [item]}, item


def test_csv_assertions_include_all_occurrences_and_bind_selection():
    source, item = selected({"type": "text/csv"}, {"download": "listing.CSV"})
    value = hints.derive(source, item)
    assert value["status"] == "resolved" and value["format"] == "csv"
    assert value["assertion_count"] == 2 and value["omitted_assertions"] == 0
    assert value["candidate_sha256"] == item["candidate_sha256"]
    source["another"] = True
    assert hints.derive(source, item)["selection_sha256"] != value["selection_sha256"]


def test_conflicts_unknown_and_overflow_never_choose_csv():
    for labels in [({"type": "text/csv"}, {"type": "application/pdf"}),
                   ({"name": "prices.csv"}, {"download": "property.pdf"}),
                   ({"type": "text/csv; charset=utf-8"},),
                   tuple({"type": "text/csv"} for _ in range(65))]:
        source, item = selected(*labels)
        result = hints.derive(source, item)
        assert result["status"] == "unresolved" and result["format"] is None
    assert result["omitted_assertions"] == 1 and len(result["assertions"]) == 64


def test_prose_is_not_a_format_assertion():
    source, item = selected({"title": "Download CSV of property details"})
    assert hints.derive(source, item) is None
