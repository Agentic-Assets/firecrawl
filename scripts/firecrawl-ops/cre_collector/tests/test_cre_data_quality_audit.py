import cre_data_quality_audit as audit


def test_build_findings_flags_failures_and_warnings():
    results = {
        "object_health": [{"check_name": "missing_tables", "count": 1, "detail": ["x"]}],
        "orphans": [{"check_name": "contacts_without_parent", "count": 2}],
        "queue_health": [{"dead_rows": 1, "stale_claimed_rows": 0}],
        "duplicate_external_ids": [{"source_key": "cbre", "external_id": "x"}],
        "listing_quality_by_source": [
            {
                "source_key": "cbre",
                "rows_checked": 10,
                "bad_source_url": 1,
                "bad_canonical_url": 0,
                "missing_title": 0,
                "missing_raw_data": 0,
                "missing_transaction_type": 0,
                "missing_property_type": 0,
                "invalid_state": 0,
                "impossible_lat": 0,
                "impossible_lng": 0,
                "missing_state": 1,
                "missing_coords": 9,
            }
        ],
        "numeric_anomalies": [{"field_name": "lease_rate_min"}],
        "bad_child_urls": [{"issue": "avatar_url"}],
        "duplicate_source_urls": [{"source_key": "cbre"}],
        "source_index_health": [{"source_key": "cbre", "stale_last_enumerated": 3}],
        "recent_jobs": [{"brokerage_slug": "cbre", "status": "failed", "errors_count": 1}],
        "search_smoke": [{"smoke_name": "x", "result_rows": 0}],
    }

    findings = audit.build_findings(results)
    severities = [item["severity"] for item in findings]

    assert "FAIL" in severities
    assert "WARN" in severities
    assert "INFO" in severities


def test_markdown_table_escapes_pipes_and_limits_rows():
    rows = [{"name": "a|b", "value": "one\ntwo"}, {"name": "c", "value": "d"}]

    rendered = audit.markdown_table(rows, max_rows=1)

    assert "a\\|b" in rendered
    assert "one two" in rendered
    assert "Showing 1 of 2 rows" in rendered
