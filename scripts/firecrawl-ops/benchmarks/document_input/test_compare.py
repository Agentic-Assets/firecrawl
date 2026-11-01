"""Offline measurement tests with synthetic hashes and source-review decisions."""

import copy

import compare
import pytest


def gold():
    return compare.runner.canonical(
        {
            "schema": "document-input-adjudication-gold/v1",
            "manifest_sha256": "a" * 64,
            "documents": [
                {
                    "id": "doc",
                    "pdf_sha256": "c" * 64,
                    "facts": [{"id": "fact-1"}, {"id": "fact-2"}],
                    "tables": [],
                }
            ],
        }
    )


def report(effort="high", *, suffix="a", unknown=False):
    return {
        "schema": "native-pdf-benchmark-report/v1",
        "manifest_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "rows": [
            {
                "document_id": "doc",
                "pdf_sha256": "c" * 64,
                "model": "example/model",
                "provider": "example",
                "effort": effort,
                "max_tokens": 8000,
                "result_sha256": suffix * 64,
                "cost_usd": None if unknown else 0.02,
                "elapsed_seconds": 2,
                "http_status": 200,
                "strict_usable": False,
                "usable_after_fence": True,
            }
        ],
    }


def review():
    base = {
        "document_id": "doc",
        "source_pdf_sha256": "c" * 64,
        "model": "example/model",
        "requested_provider": "example",
        "result_sha256": "a" * 64,
        "gold_sha256": compare.runner.digest(gold()),
        "kind": "fact",
        "strict_decision": "unscorable",
        "semantic_view": "exact_outer_fence_recovery",
    }
    return {
        "decisions": [
            {**base, "probe_id": "fact-1", "semantic_decision": "clean_correct"},
            {
                **base,
                "probe_id": "fact-2",
                "strict_decision": "uncertain_reference",
                "semantic_decision": "uncertain_reference",
            },
        ]
    }


def test_paired_comparison_keeps_unknown_cost_and_reference_uncertainty():
    result = compare.generate(
        [report(), report("xhigh", suffix="e", unknown=True)],
        [review()],
        gold_raw=gold(),
    )
    a, b = result["summary"]
    assert a["total_cost_usd"] == 0.02 and b["total_cost_usd"] is None
    assert b["cost_unknown_count"] == 1
    assert a["strict_usable"] == 0 and a["usable_after_fence"] == 1
    assert a["semantic_decision"]["fact"]["reference_targets"] == 2
    assert a["semantic_decision"]["fact"]["scorable_targets"] == 1
    assert a["strict_decision"]["fact"]["scorable_targets"] == 1
    assert a["format_and_selected_reference_pass_documents"] == 1
    assert a["cost_per_format_and_selected_reference_pass_usd"] == 0.02
    assert result["paired"][0]["paired_known_cost_count"] == 0


def test_percentile_conventions_are_explicit():
    result = compare.percentiles(list(range(1, 31)))
    assert result == {"median": 15.5, "p90_nearest_rank": 27, "p90_interpolated": 27.1}
    assert compare.percentiles([])["median"] is None


@pytest.mark.parametrize(
    "mutation", ["pdf", "manifest", "prompt", "duplicate", "negative_cost"]
)
def test_incompatible_or_duplicate_measurements_refused(mutation):
    a, b = report(), report("xhigh", suffix="e")
    if mutation in {"manifest", "prompt"}:
        b[mutation + "_sha256"] = "f" * 64
    elif mutation == "pdf":
        b["rows"][0]["pdf_sha256"] = "f" * 64
    elif mutation == "duplicate":
        a["rows"].append(copy.deepcopy(a["rows"][0]))
    else:
        b["rows"][0]["cost_usd"] = -1
    with pytest.raises(ValueError):
        compare.generate([a, b])


@pytest.mark.parametrize(
    "mutation", ["source", "result", "duplicate", "gold", "decision"]
)
def test_review_binding_and_disposition_validation(mutation):
    evidence = review()
    if mutation == "source":
        evidence["decisions"][0]["source_pdf_sha256"] = "wrong"
    elif mutation == "result":
        evidence["decisions"][0]["result_sha256"] = "wrong"
    elif mutation == "duplicate":
        evidence["decisions"].append(copy.deepcopy(evidence["decisions"][0]))
    elif mutation == "gold":
        evidence["decisions"][0]["gold_sha256"] = "different"
    else:
        evidence["decisions"][0]["semantic_decision"] = "unsupported_guess"
    with pytest.raises(ValueError):
        compare.generate([report()], [evidence], gold_raw=gold())


def test_incomplete_reference_review_never_counts_as_document_pass():
    evidence = review()
    evidence["decisions"] = evidence["decisions"][:1]
    result = compare.generate([report()], [evidence], gold_raw=gold())["summary"][0]
    assert result["reviewed_documents"] == 1
    assert result["complete_reference_review_documents"] == 0
    assert result["format_and_selected_reference_pass_documents"] == 0
    assert result["cost_per_format_and_selected_reference_pass_usd"] is None
    with pytest.raises(ValueError, match="frozen gold required"):
        compare.generate([report()], [evidence])


@pytest.mark.parametrize(
    "value,canonical",
    [
        ("correct_with_conflict", "correct_with_conflict"),
        ("wrong", "wrong_unspecified"),
        ("uncertain", "uncertain_output"),
        ("incorrect_association", "wrong_association"),
        ("incorrect_value", "wrong_value"),
    ],
)
def test_conflict_and_legacy_error_taxonomy_never_qualifies_as_clean(value, canonical):
    evidence = review()
    evidence["decisions"][0]["semantic_decision"] = value
    result = compare.generate([report()], [evidence], gold_raw=gold())["summary"][0]
    assert result["semantic_decision"]["fact"]["counts"][canonical] == 1
    assert result["format_and_selected_reference_pass_documents"] == 0


def test_raw_http_speed_wins_are_distinct_from_successful_task_comparison():
    a, b = report(), report("xhigh", suffix="e")
    b["rows"][0].update({"usable_after_fence": False, "elapsed_seconds": 0.1})
    result = compare.generate([a, b])["paired"][0]
    assert result["all_http_200_b_faster_count"] == 1
    assert result["both_format_and_complete_selected_reference_pass_documents"] == 0
    assert result["passing_b_faster_count"] == 0


def test_source_uncertainty_cannot_change_between_models_or_runs():
    second = copy.deepcopy(review())
    for d in second["decisions"]:
        d["result_sha256"] = "e" * 64
    second["decisions"][1].update(
        {"strict_decision": "unscorable", "semantic_decision": "clean_correct"}
    )
    with pytest.raises(ValueError, match="uncertainty differs"):
        compare.generate(
            [report(), report("xhigh", suffix="e")], [review(), second], gold_raw=gold()
        )


def test_contradictory_strict_or_semantic_decisions_are_refused():
    evidence = review()
    evidence["decisions"][0]["strict_decision"] = "clean_correct"
    with pytest.raises(ValueError, match="strict review contradicts"):
        compare.generate([report()], [evidence], gold_raw=gold())
    evidence["decisions"][0]["strict_decision"] = "unscorable"
    bad = report()
    bad["rows"][0]["usable_after_fence"] = False
    with pytest.raises(ValueError, match="readable output view"):
        compare.generate([bad], [evidence], gold_raw=gold())
    # An explicitly verified readable-object sensitivity is permitted without
    # treating its schema-invalid document as a usable whole-document pass.
    bad["rows"][0].update(
        {"field_object_readable": True, "field_object_view": "strict_json_object"}
    )
    evidence["decisions"][0]["semantic_view"] = "field_sensitivity"
    result = compare.generate([bad], [evidence], gold_raw=gold())["summary"][0]
    assert result["semantic_decision"]["fact"]["counts"]["clean_correct"] == 1
    assert result["format_and_selected_reference_pass_documents"] == 0
