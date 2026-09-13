"""Pure SQL and CLI contracts for the optional post-commit ingest summary."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cre_ingest as ci
import pytest

OBSERVED_AT = "2026-09-13T20:00:00+00:00"
SUMMARY_SEPARATOR = "\n\n\\echo ''\n\\echo '=== credeals.cre_listings after ingest ==='"


def _listing_row(source: str, index: int, transaction: str) -> dict:
    listing = {
        "sourceKey": source,
        "id": f"listing-{index}",
        "url": f"https://example.test/{source}/{index}",
    }
    if transaction == "sale_or_lease":
        listing["transactionType"] = "For Sale and Lease"
    else:
        listing["transactionMode"] = transaction
    row = ci.to_row(listing, {}, OBSERVED_AT)
    assert row is not None
    return row


def _job_meta(rows: list[dict]) -> list[dict]:
    return [
        {
            "slug": slug,
            "discovered": sum(row["slug"] == slug for row in rows),
            "saved": sum(row["slug"] == slug for row in rows),
            "errors": 0,
            "notes": None,
            "finished_at": OBSERVED_AT,
        }
        for slug in sorted({row["slug"] for row in rows})
    ]


@pytest.mark.parametrize(
    "source_transactions,mark_missing_slugs",
    [
        pytest.param([], set(), id="empty-stage"),
        pytest.param([("svn", "sale")], set(), id="one-sale-row"),
        pytest.param(
            [("jll", "lease"), ("cbre", "sale_or_lease")],
            set(),
            id="two-sources-two-transactions",
        ),
        pytest.param(
            [("svn", "sale"), ("svn", "lease"), ("jll", "sale_or_lease")],
            {"svn"},
            id="three-rows-mark-missing",
        ),
    ],
)
def test_summary_skip_changes_only_sql_after_commit(
    source_transactions, mark_missing_slugs
):
    rows = [
        _listing_row(source, index, transaction)
        for index, (source, transaction) in enumerate(source_transactions, start=1)
    ]
    kwargs = {
        "artifact_run_key": f"ingest:v1:{'a' * 64}",
        "finished_at": OBSERVED_AT,
    }

    default_sql = ci.build_sql(
        rows,
        _job_meta(rows),
        OBSERVED_AT,
        mark_missing_slugs,
        **kwargs,
    )
    skipped_sql = ci.build_sql(
        rows,
        _job_meta(rows),
        OBSERVED_AT,
        mark_missing_slugs,
        skip_post_commit_summary=True,
        **kwargs,
    )

    transaction_sql, separator, summary_sql = default_sql.partition(SUMMARY_SEPARATOR)
    assert separator == SUMMARY_SEPARATOR
    assert transaction_sql.endswith("COMMIT;")
    assert skipped_sql.rstrip("\n") == transaction_sql
    assert default_sql[: len(transaction_sql)] == skipped_sql[: len(transaction_sql)]
    assert "GROUP BY 1 ORDER BY 1;" in summary_sql
    assert "credeals.cre_listings after ingest" not in skipped_sql


def _write_artifact(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "runMeta": {
                    "mode": "full",
                    "startedAt": OBSERVED_AT,
                    "finishedAt": OBSERVED_AT,
                },
                "sources": [
                    {
                        "sourceKey": "svn",
                        "transaction": "sale",
                        "supported": True,
                        "listingsCollected": 1,
                        "truncated": False,
                    }
                ],
                "listings": [
                    {
                        "sourceKey": "svn",
                        "transactionMode": "sale",
                        "id": "cli-listing",
                        "url": "https://example.test/svn/cli-listing",
                    }
                ],
                "brokers": [],
            }
        ),
        encoding="utf-8",
    )


def test_cli_flag_omits_only_the_post_commit_summary(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact.json"
    _write_artifact(artifact)
    default_dir = tmp_path / "default"
    skipped_dir = tmp_path / "skipped"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(artifact),
            "--dry-run",
            "--keep-artifacts",
            str(default_dir),
        ],
    )
    ci.main()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cre_ingest.py",
            "--in",
            str(artifact),
            "--dry-run",
            "--keep-artifacts",
            str(skipped_dir),
            "--skip-post-commit-summary",
        ],
    )
    ci.main()

    default_sql = (default_dir / "ingest.sql").read_text(encoding="utf-8")
    skipped_sql = (skipped_dir / "ingest.sql").read_text(encoding="utf-8")
    transaction_sql, separator, _summary_sql = default_sql.partition(SUMMARY_SEPARATOR)
    assert separator == SUMMARY_SEPARATOR
    assert skipped_sql.rstrip("\n") == transaction_sql
