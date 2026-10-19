"""Finite direct-provider decoding and historical coverage contracts."""

import json
import shutil
import subprocess
from pathlib import Path

import cre_performance_report as report
import pytest

from test_cre_performance_report import (
    COMMAND,
    METRICS,
    NOW,
    RUN,
    checkpoint,
    journal,
    snapshot,
)


TWO_SOURCE_COVERAGE = "essex_pyramid_only_other_direct_sources_unknown"
LEGACY_COVERAGE = "essex_only_other_direct_sources_unknown"


def direct(
    source="pyramid-brokerage", transaction="sale", role="current_archive", count=1
):
    return {
        "source": source,
        "transaction": transaction,
        "role": role,
        "attempts_started": count,
        "attempts_completed": count,
        "succeeded": count,
        "failed": 0,
        "active_locally_awaited": 0,
        "summed_latency_ms": count * 10,
        "status_counts": {"200": count},
        "private_url": "https://private.invalid/do-not-publish",
        "body": "private-body-sentinel",
        "native_id": "private-native-id-sentinel",
        "error": "private-error-sentinel",
    }


def decoded(records):
    return report._direct_provider_metrics({"by_source_transaction_role": records})


def saved_report(tmp_path, records, coverage=None):
    run = checkpoint(tmp_path)
    journal(run)
    path = snapshot(run)
    value = json.loads(path.read_text())
    value["metrics"]["direct_provider_requests"] = {
        "by_source_transaction_role": records
    }
    if coverage is not None:
        value["coverage"] = {"direct_provider_requests": coverage}
    path.write_text(json.dumps(value))
    return report.build_report(run, now=NOW)


@pytest.mark.parametrize("role", ["current_archive", "detail"])
def test_pyramid_direct_roles_survive_real_decoder(role):
    rows = decoded([direct(role=role)])
    assert len(rows) == 1
    assert rows[0]["source"] == "pyramid-brokerage"
    assert rows[0]["role"] == role
    assert rows[0]["status_counts"] == {"200": 1}
    assert "private_url" not in rows[0]
    assert "sentinel" not in json.dumps(rows)


@pytest.mark.parametrize(
    ("source", "role"),
    [
        ("pyramid-brokerage", "closed_archive"),
        ("pyramid-brokerage", "sitemap"),
        ("other-provider", "detail"),
        ("pyramid-brokerage", "https://private.invalid"),
    ],
)
def test_unsupported_direct_source_role_is_not_published(source, role):
    assert decoded([direct(source=source, role=role)]) == []


@pytest.mark.parametrize("field", ["source", "role", "transaction"])
@pytest.mark.parametrize("value", [[], {}, None, True])
def test_malformed_direct_group_key_is_ignored_without_crashing(field, value):
    record = direct()
    record[field] = value
    assert decoded([record]) == []


def test_direct_cardinality_allows_exact_finite_eighteen_rows():
    records = [
        direct(source, transaction, role)
        for source, roles in (
            (
                "essex-realty",
                ["current_archive", "closed_archive", "sitemap", "detail"],
            ),
            ("pyramid-brokerage", ["current_archive", "detail"]),
        )
        for transaction in ("sale", "lease", "unknown")
        for role in roles
    ]
    assert len(decoded(records)) == 18
    assert decoded([*records, direct()]) == []


def test_direct_source_transaction_keys_do_not_collide():
    rows = decoded(
        [
            direct("essex-realty", count=2),
            direct(count=3),
            direct(transaction="lease", count=5),
        ]
    )
    assert {
        (row["source"], row["transaction"]): row["attempts_started"] for row in rows
    } == {
        ("essex-realty", "sale"): 2,
        ("pyramid-brokerage", "sale"): 3,
        ("pyramid-brokerage", "lease"): 5,
    }


def test_saved_two_source_coverage_is_retained_even_before_first_request(tmp_path):
    result = saved_report(tmp_path, [], TWO_SOURCE_COVERAGE)
    scrape = result["sources"][0]["commands"][0]["scrape"]
    assert scrape["coverage"]["direct_provider_requests"] == TWO_SOURCE_COVERAGE
    assert scrape["direct_provider_requests"] == []


@pytest.mark.parametrize("coverage", [None, LEGACY_COVERAGE])
def test_historical_essex_report_is_not_upgraded_by_reader(tmp_path, coverage):
    result = saved_report(tmp_path, [direct("essex-realty")], coverage)
    scrape = result["sources"][0]["commands"][0]["scrape"]
    assert scrape["coverage"]["direct_provider_requests"] == LEGACY_COVERAGE
    text = report.render(result)
    assert "Essex direct sale/current_archive" in text
    assert "Pyramid direct" not in text


def test_two_source_report_renders_bounded_correct_source_labels(tmp_path):
    result = saved_report(
        tmp_path, [direct("essex-realty"), direct()], TWO_SOURCE_COVERAGE
    )
    scrape = result["sources"][0]["commands"][0]["scrape"]
    assert scrape["coverage"]["direct_provider_requests"] == TWO_SOURCE_COVERAGE
    text = report.render(result)
    assert "Essex direct sale/current_archive" in text
    assert "Pyramid direct sale/current_archive" in text
    assert "only Essex direct requests are instrumented" not in text
    assert "private.invalid" not in text
    assert "sentinel" not in text


def test_unknown_saved_coverage_is_not_promoted_to_two_source(tmp_path):
    result = saved_report(tmp_path, [], "all_sources_instrumented")
    scrape = result["sources"][0]["commands"][0]["scrape"]
    assert scrape["coverage"]["direct_provider_requests"] == "unknown_uninstrumented"


def test_real_mixed_typescript_producer_is_consumed_and_rendered(tmp_path):
    collector = Path(__file__).resolve().parents[1]
    node = shutil.which("node")
    if node is None or not (collector / "node_modules/tsx").is_dir():
        pytest.skip(
            "cross-language contract requires installed collector Node dependencies"
        )
    run = checkpoint(tmp_path)
    journal(run)
    script = """
      import { createPerformanceRecorder, setPerformanceRecorderForTests,
        withPerformanceSource, recordDirectProviderAttemptStarted,
        recordDirectProviderAttemptCompleted } from './lib/performance.ts';
      const recorder = createPerformanceRecorder({
        path: process.argv[1], runId: process.argv[2], commandId: process.argv[3],
        nowIso: () => '2026-09-13T12:00:20Z'
      });
      setPerformanceRecorderForTests(recorder);
      for (const [source, transaction] of [
        ['essex-realty','sale'], ['pyramid-brokerage','sale'],
        ['pyramid-brokerage','lease']
      ]) {
        await withPerformanceSource(source, transaction, async () => {
          const token = recordDirectProviderAttemptStarted('current_archive', source);
          recordDirectProviderAttemptCompleted(token, {outcome:'succeeded',status:200});
        });
      }
      recorder.flush(true);
    """
    completed = subprocess.run(
        [
            node,
            "--import",
            "tsx",
            "--input-type=module",
            "-e",
            script,
            str(run / "logs" / METRICS),
            RUN,
            COMMAND,
        ],
        cwd=collector,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = report.build_report(run, now=NOW)
    scrape = result["sources"][0]["commands"][0]["scrape"]
    assert scrape["coverage"]["direct_provider_requests"] == TWO_SOURCE_COVERAGE
    assert {
        (row["source"], row["transaction"]): row["attempts_completed"]
        for row in scrape["direct_provider_requests"]
    } == {
        ("essex-realty", "sale"): 1,
        ("pyramid-brokerage", "sale"): 1,
        ("pyramid-brokerage", "lease"): 1,
    }
    text = report.render(result)
    assert "Pyramid direct sale/current_archive" in text
    assert "Pyramid direct lease/current_archive" in text
