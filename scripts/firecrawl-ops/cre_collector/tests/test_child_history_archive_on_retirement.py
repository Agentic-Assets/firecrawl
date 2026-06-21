"""
test_child_history_archive_on_retirement.py

Locks down the M2 child-history archive writes in cre_ingest.py build_sql().

At mark-missing retirement, the ingestor now snapshots the retired listings'
FINAL contacts and documents into append-only archive tables in the same
transaction as the soft-delete. This preserves "who brokered this now-sold deal"
and its final brochures after the next re-scrape's wholesale child-replace.

Images are EXCLUDED (high volume, low historical value).
Archive tables are existence-guarded (no-op until 009 is applied to prod).
In --dry-run (history_guard=False), the INSERTs are emitted unconditionally
so offline tests can assert on them directly.

Column lists tested are verbatim from spec sections 2.5 / 3.4:
- contacts archive: (source_listing_id, name, title, email, phone,
    brokerage_name, profile_url, avatar_url, vcard_url, is_primary)
- documents archive: (source_listing_id, doc_type, title, url)

Subprocess cases use --dry-run --keep-artifacts for the positive mark-missing
path (singleton svn, mirror case 3 from test_ingest_mark_missing.py).
"""

import json
import os
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COLLECTOR_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FINISHED_AT = "2026-06-15T00:00:00.000Z"
_STARTED_AT = "2026-06-15T00:00:00.000Z"
_LISTING_COUNT = 3

# Exact column lists from spec sections 2.5 / 3.4.
_CONTACTS_COL_LIST = (
    "(source_listing_id, name, title, email, phone, brokerage_name,\n"
    "     profile_url, avatar_url, vcard_url, is_primary)"
)
_DOCUMENTS_COL_LIST = "(source_listing_id, doc_type, title, url)"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_entry(source_key, count=_LISTING_COUNT, error=None):
    entry = {
        "sourceKey": source_key,
        "transaction": "sale",
        "listingsCollected": count,
    }
    if error:
        entry["error"] = error
    return entry


def _svn_listing(idx=0):
    return {
        "sourceKey": "svn",
        "url": f"https://www.svn.com/property?propertyId=svn-{idx:04d}-sale",
        "id": f"svn-{idx:04d}",
        "transactionMode": "sale",
    }


def _write_artifact(payload, tmp_path, name="artifact.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload))
    return str(path)


def _run_dry(artifact_path, tmp_path):
    artifacts_dir = str(tmp_path / "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "cre_ingest.py",
            "--in", artifact_path,
            "--mark-missing",
            "--dry-run",
            "--keep-artifacts", artifacts_dir,
            "--mark-missing-floor", "1",
        ],
        cwd=_COLLECTOR_DIR,
        capture_output=True,
        text=True,
    )
    sql_path = os.path.join(artifacts_dir, "ingest.sql")
    sql_text = None
    if os.path.isfile(sql_path):
        with open(sql_path) as f:
            sql_text = f.read()
    return result.returncode, result.stderr, sql_text


# ---------------------------------------------------------------------------
# Positive case: singleton svn -- archive INSERTs are present
# ---------------------------------------------------------------------------


class TestArchiveInsertsOnRetirement:
    """
    Singleton svn clears the mark-missing floor. The generated SQL (dry-run,
    history_guard=False) must include both archive INSERTs with the exact column
    lists and a JOIN to _retired.
    """

    def _make_artifact(self):
        return {
            "runMeta": {"startedAt": _STARTED_AT, "finishedAt": _FINISHED_AT},
            "brokers": [],
            "sources": [_source_entry("svn")],
            "listings": [_svn_listing(i) for i in range(_LISTING_COUNT)],
        }

    def test_contacts_archive_insert_present(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        rc, stderr, sql = _run_dry(art, tmp_path)

        assert rc == 0, f"ingestor exited {rc}. stderr:\n{stderr}"
        assert sql is not None
        assert "INSERT INTO credeals.cre_listing_contacts_archive" in sql, (
            "Expected INSERT INTO cre_listing_contacts_archive in mark-missing SQL."
        )

    def test_documents_archive_insert_present(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "INSERT INTO credeals.cre_listing_documents_archive" in sql, (
            "Expected INSERT INTO cre_listing_documents_archive in mark-missing SQL."
        )

    def test_contacts_archive_column_list_exact(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert _CONTACTS_COL_LIST in sql, (
            f"Expected exact contacts archive column list:\n{_CONTACTS_COL_LIST!r}\n"
            f"not found in SQL."
        )

    def test_documents_archive_column_list_exact(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert _DOCUMENTS_COL_LIST in sql, (
            f"Expected exact documents archive column list:\n{_DOCUMENTS_COL_LIST!r}\n"
            f"not found in SQL."
        )

    def test_archive_joins_retired_table(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "JOIN _retired r" in sql, (
            "Expected JOIN _retired r in archive INSERT SQL (contacts or documents)."
        )

    def test_no_images_archive(self, tmp_path):
        payload = self._make_artifact()
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "images_archive" not in sql, (
            "Images archive reference found in SQL. Images must be excluded from M2."
        )
        assert "INSERT INTO credeals.cre_listing_images_archive" not in sql, (
            "Images archive INSERT found. Images are explicitly excluded."
        )


# ---------------------------------------------------------------------------
# Negative case: archive INSERTs absent when no brokerage is eligible
# ---------------------------------------------------------------------------


class TestNoArchiveWhenMarkMissingBlocked:
    """
    When the only brokerage is blocked (cbre without cbre-dealflow), neither
    the contacts archive nor the documents archive INSERT should appear.
    """

    def test_no_contacts_archive_when_blocked(self, tmp_path):
        listings = [
            {
                "sourceKey": "cbre",
                "url": f"https://www.cbre.com/p/test-cbre-{i:04d}",
                "id": f"test-cbre-{i:04d}",
                "transactionMode": "sale",
            }
            for i in range(_LISTING_COUNT)
        ]
        payload = {
            "runMeta": {"startedAt": _STARTED_AT, "finishedAt": _FINISHED_AT},
            "brokers": [],
            "sources": [_source_entry("cbre")],
            "listings": listings,
        }
        art = _write_artifact(payload, tmp_path)
        _, _, sql = _run_dry(art, tmp_path)

        assert sql is not None
        assert "INSERT INTO credeals.cre_listing_contacts_archive" not in sql, (
            "Contacts archive INSERT found even though mark-missing was blocked."
        )
        assert "INSERT INTO credeals.cre_listing_documents_archive" not in sql, (
            "Documents archive INSERT found even though mark-missing was blocked."
        )
